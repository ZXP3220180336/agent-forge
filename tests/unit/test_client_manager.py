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
