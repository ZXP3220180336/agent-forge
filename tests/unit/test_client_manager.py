"""
ClientManager 单元测试

覆盖旧 client 关闭追踪（register_config 热重配时的连接池释放）：
    - 无运行事件循环（纯注册阶段，同步上下文）：旧 client 进入 _pending_closes
      追踪，close_all 统一关闭，不静默丢失连接池
    - 有运行事件循环：旧 client 通过 ensure_future 后台关闭（不阻塞注册）
"""

import asyncio

import pytest

from app.services.llm import ClientManager


class _FakeClient:
    """模拟 AsyncOpenAI：记录 close() 调用次数。"""

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def _register_with_old_client(key: str) -> _FakeClient:
    """注册配置并把一个 fake client 塞进 _instances（模拟已懒加载）。"""
    ClientManager.register_config(key, api_key="k", base_url="http://x", model="m")
    old = _FakeClient()
    ClientManager._instances[key] = old
    return old


@pytest.fixture(autouse=True)
def _clean_state():
    """每个测试后清理 ClientManager 状态，避免跨测试污染。"""
    yield
    ClientManager._instances.clear()
    ClientManager._configs.clear()
    ClientManager._pending_closes.clear()
    ClientManager._closing_tasks.clear()


def test_register_no_loop_tracks_old_client_in_pending():
    """无运行事件循环（同步上下文）→ 旧 client 进入 _pending_closes，不静默丢失。

    修复前：`except RuntimeError: pass` 静默忽略，旧连接池泄漏。
    修复后：放入待关闭列表由 close_all 统一关闭，可追踪。
    """
    old = _register_with_old_client("main")

    # 同步函数内无运行事件循环 → ensure_future 抛 RuntimeError → 走 pending
    ClientManager.register_config("main", api_key="k", base_url="http://x", model="m")

    assert "main" not in ClientManager._instances, "旧实例应从缓存移除"
    assert old in ClientManager._pending_closes, (
        "无 loop 时旧 client 应进入待关闭列表（不静默忽略）"
    )
    assert old.closed == 0, "尚未调用 close（等待 close_all 统一关闭）"


def test_close_all_closes_pending_old_clients():
    """close_all 统一关闭无 loop 阶段积累的待关闭旧 client。"""
    old = _register_with_old_client("main")
    ClientManager.register_config("main", api_key="k", base_url="http://x", model="m")
    assert old in ClientManager._pending_closes

    asyncio.run(ClientManager.close_all())

    assert old.closed == 1, "close_all 应关闭待关闭的旧 client"
    assert ClientManager._pending_closes == [], "close_all 后清空待关闭列表"


async def test_register_with_loop_closes_old_async():
    """有运行事件循环 → 旧 client 通过 ensure_future 后台关闭（不进入 pending）。"""
    old = _register_with_old_client("main")

    # async 测试内存在运行 loop → ensure_future 生效
    ClientManager.register_config("main", api_key="k", base_url="http://x", model="m")

    assert old not in ClientManager._instances, "旧实例应从缓存移除"
    assert old not in ClientManager._pending_closes, (
        "有 loop 时走 ensure_future，不进入 pending"
    )
    # 让 ensure_future 的后台关闭任务跑完
    await asyncio.sleep(0.05)
    assert old.closed == 1, "有 loop 时旧 client 应被后台关闭"


async def test_register_with_loop_tracks_closing_task():
    """有运行事件循环 → 后台 close task 被记录到 _closing_tasks，close_all 等待。"""
    old = _register_with_old_client("main")

    ClientManager.register_config("main", api_key="k", base_url="http://x", model="m")

    assert len(ClientManager._closing_tasks) == 1, "后台 close task 应被追踪"
    assert not ClientManager._closing_tasks[0].done(), "task 尚未完成（可被 close_all 等待）"

    # close_all 等待后台 task 完成后清空，不留 pending task
    await ClientManager.close_all()

    assert old.closed == 1, "close_all 应确保旧 client 被关闭"
    assert ClientManager._closing_tasks == [], "close_all 后清空后台 task 列表"


class _SlowCloseClient(_FakeClient):
    """close 阻塞一段时间，模拟后台关闭未完成场景。"""

    async def close(self) -> None:
        await asyncio.sleep(0.05)
        await super().close()


async def test_close_all_awaits_pending_close_tasks():
    """close_all 等待未完成的后台 close task（避免 task 泄漏 + 竞态）。"""
    slow = _SlowCloseClient()
    ClientManager._instances["main"] = slow
    # 手动触发热切换：有 loop → 后台 task，不进入 pending_closes
    ClientManager.register_config("main", api_key="k", base_url="http://x", model="m")

    assert slow not in ClientManager._pending_closes, "有 loop 时走后台 task"
    assert len(ClientManager._closing_tasks) == 1, "后台 close task 应被追踪"

    # 立即 close_all（task 尚未完成）→ 应等待其完成而非丢弃
    await ClientManager.close_all()

    assert slow.closed == 1, "close_all 应等待后台 task 完成关闭"
    assert ClientManager._closing_tasks == [], "close_all 后清空后台 task 列表"


class _SlowThrowCloseClient(_SlowCloseClient):
    """close 阻塞后抛异常，模拟后台 close task 失败。"""

    async def close(self) -> None:
        await asyncio.sleep(0.05)
        self.closed += 1
        raise RuntimeError("close failed")


async def test_close_all_isolates_pending_close_task_exception():
    """后台 close task 抛异常 → close_all 用 return_exceptions 隔离，不中断清理。"""
    bad = _SlowThrowCloseClient()
    good = _FakeClient()
    ClientManager._instances["main"] = bad
    ClientManager.register_config("main", api_key="k", base_url="http://x", model="m")
    ClientManager._instances["backup"] = good

    await ClientManager.close_all()

    assert bad.closed == 1, "异常 client 自身 close 仍被调用"
    assert good.closed == 1, "异常 task 不影响其余 client 关闭"
    assert ClientManager._closing_tasks == [], "close_all 后清空后台 task 列表"
    assert ClientManager._instances == {}, "close_all 后清空实例缓存"


class _ConcurrentCloseClient(_FakeClient):
    """close 时触发 register_config 修改 _instances（模拟并发热重配）。"""

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    async def close(self) -> None:
        # 在 close_all 迭代 _instances 的 await 间隙修改字典 → 迭代器失效
        ClientManager.register_config(
            self._key, api_key="k2", base_url="http://x", model="m2"
        )
        await super().close()


async def test_close_all_snapshots_instances_before_iterating():
    """close_all 迭代期间字典被修改 → 不抛 RuntimeError（先快照再逐个关）。

    修复前：for client in cls._instances.values(): await client.close() 中 await
    释放事件循环控制权，若 close 期间 register_config()/close_client() 修改字典，
    迭代器失效抛 RuntimeError: dictionary changed size during iteration，清理中断。
    """
    # 两个实例，第一个 close 时触发 register_config 修改 _instances
    a = _ConcurrentCloseClient("main")
    b = _FakeClient()
    ClientManager._instances.update({"main": a, "backup": b})

    await ClientManager.close_all()

    assert a.closed == 1, "迭代期间字典被改也应收尾（快照后逐关）"
    assert b.closed == 1, "后续实例应继续被关闭"
    assert ClientManager._instances == {}, "close_all 后清空实例缓存"


class _ThrowOnCloseClient(_FakeClient):
    """close() 抛异常，模拟连接池关闭失败。"""

    async def close(self) -> None:
        self.closed += 1
        raise RuntimeError("close failed")


async def test_close_all_isolates_single_close_failure():
    """某个 client.close() 抛异常 → 后续 client 与 _pending_closes 仍被关闭。

    修复前：await client.close() 异常中断循环，后续 client 与 _pending_closes
    均不关闭 → 连接池泄漏。
    """
    bad = _ThrowOnCloseClient()
    good = _FakeClient()
    pending = _FakeClient()
    ClientManager._instances.update({"main": bad, "backup": good})
    ClientManager._pending_closes.append(pending)

    await ClientManager.close_all()

    assert bad.closed == 1, "异常 client 自身 close 仍被调用"
    assert good.closed == 1, "异常后后续实例应继续被关闭"
    assert pending.closed == 1, "异常后 _pending_closes 应继续被关闭"
    assert ClientManager._instances == {}, "close_all 后清空实例缓存"
    assert ClientManager._pending_closes == [], "close_all 后清空待关闭列表"
