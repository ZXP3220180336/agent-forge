"""execution_control 等待原语直测（LLM-045）。

直测 `wait_with_execution_control` / `await_with_execution_control` 的 abort 分派语义，
重点覆盖 LLM-045：abort 判赢后被控任务吞掉取消、以值正常收尾时，helper 应返回该
迟回值（交调用方接管），而非丢弃后抛类型化信号。

泄漏断言以**确定性信号**为主（factory finally / CancelledError 计数 / 返回对象
identity / 有限超时），`asyncio.all_tasks()` 差集仅作辅助并排除当前测试 task。
"""

import asyncio
import time

import pytest

from app.integration.llm.errors import _DeadlineExceeded, _StreamCancel
from app.integration.llm.execution_control import (
    await_with_execution_control,
    wait_with_execution_control,
)

SENTINEL = object()


def _leaked(before: set[asyncio.Task]) -> list:
    """辅助泄漏检查：除当前测试 task 外仍有未完成 task → 泄漏。"""
    cur = asyncio.current_task()
    return [t for t in (asyncio.all_tasks() - before) if t is not cur and not t.done()]


# =====================================================================
# await_with_execution_control —— LLM-045 迟回值返回
# =====================================================================


async def test_await_swallow_cancel_returns_value_on_cancel_event():
    """cancel：factory 吞掉取消后正常返回 → helper 返回该迟回值（不抛 _StreamCancel）。"""
    cancel_event = asyncio.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    cancels = 0

    async def factory():
        nonlocal cancels
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancels += 1  # 吞取消，返回已取得的资源
            return SENTINEL
        finally:
            finished.set()

    before = set(asyncio.all_tasks())
    task = asyncio.ensure_future(await_with_execution_control(factory, cancel_event=cancel_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    cancel_event.set()
    result = await asyncio.wait_for(task, timeout=1)
    assert result is SENTINEL
    assert cancels == 1
    assert finished.is_set()
    assert _leaked(before) == []


async def test_await_swallow_cancel_returns_value_on_deadline():
    """deadline：factory 吞取消正常返回 → helper 返回迟回值（不抛 _DeadlineExceeded）。"""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def factory():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return SENTINEL
        finally:
            finished.set()

    before = set(asyncio.all_tasks())
    # deadline 落在 factory 启动之后（先握手再等到期，避免只测入口快检）
    task = asyncio.ensure_future(await_with_execution_control(factory, deadline=time.monotonic() + 0.2))
    await asyncio.wait_for(started.wait(), timeout=1)
    result = await asyncio.wait_for(task, timeout=2)
    assert result is SENTINEL
    assert finished.is_set()
    assert _leaked(before) == []


async def test_await_cooperating_cancel_reraises_cancel_signal():
    """cancel：factory 配合取消（内部清理后重抛）→ helper 抛 _StreamCancel。"""
    cancel_event = asyncio.Event()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def factory():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)  # 模拟清理
            cleaned.set()
            raise  # 配合：重抛取消

    task = asyncio.ensure_future(await_with_execution_control(factory, cancel_event=cancel_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    cancel_event.set()
    with pytest.raises(_StreamCancel):
        await asyncio.wait_for(task, timeout=1)
    assert cleaned.is_set()


async def test_await_cooperating_cancel_reraises_deadline_signal():
    """deadline：factory 配合取消 → helper 抛 _DeadlineExceeded。"""
    started = asyncio.Event()

    async def factory():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise  # 配合：重抛取消

    task = asyncio.ensure_future(await_with_execution_control(factory, deadline=time.monotonic() + 0.2))
    await asyncio.wait_for(started.wait(), timeout=1)
    with pytest.raises(_DeadlineExceeded):
        await asyncio.wait_for(task, timeout=2)


async def test_await_cleanup_exception_after_cancel_still_abort_wins():
    """取消后清理期抛普通异常 → abort 信号优先，异常不漏出。"""
    started = asyncio.Event()

    async def factory():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup boom")  # 清理期真实异常

    task = asyncio.ensure_future(await_with_execution_control(factory, deadline=time.monotonic() + 0.2))
    await asyncio.wait_for(started.wait(), timeout=1)
    with pytest.raises(_DeadlineExceeded):
        await asyncio.wait_for(task, timeout=2)


async def test_await_task_finishes_first_returns_and_reclaims_abort_waiter():
    """task 先完成（abort waiter 永不完成）→ 返回 task 结果，waiter 被回收。"""
    before = set(asyncio.all_tasks())
    result = await await_with_execution_control(lambda: asyncio.sleep(0, result=SENTINEL))
    assert result is SENTINEL
    assert _leaked(before) == []


async def test_await_simultaneous_task_and_abort_returns_task_value():
    """task 与 abort 同时完成（竞态）→ 返回 task 结果（不丢返回值，既有契约）。"""
    cancel_event = asyncio.Event()
    started = asyncio.Event()

    async def factory():
        started.set()
        await asyncio.sleep(0)
        cancel_event.set()  # 与返回同一 tick：镜像 reserve 竞态
        return SENTINEL

    task = asyncio.ensure_future(await_with_execution_control(factory, cancel_event=cancel_event))
    await asyncio.wait_for(started.wait(), timeout=1)
    result = await asyncio.wait_for(task, timeout=1)
    assert result is SENTINEL


async def test_await_entry_cancel_skips_factory():
    """入口已取消 → 抛 _StreamCancel，factory 不执行。"""
    cancel_event = asyncio.Event()
    cancel_event.set()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return SENTINEL

    with pytest.raises(_StreamCancel):
        await await_with_execution_control(factory, cancel_event=cancel_event)
    assert calls == 0


async def test_await_entry_deadline_skips_factory():
    """入口已到期 → 抛 _DeadlineExceeded，factory 不执行。"""
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return SENTINEL

    with pytest.raises(_DeadlineExceeded):
        await await_with_execution_control(factory, deadline=time.monotonic() - 1)
    assert calls == 0


# =====================================================================
# wait_with_execution_control —— 基本回归
# =====================================================================


async def test_wait_full_delay_returns_when_no_signal():
    """无信号：睡满 delay 后正常返回。"""
    t0 = time.monotonic()
    await wait_with_execution_control(0.01)
    assert time.monotonic() - t0 >= 0.008


async def test_wait_cancel_interrupts_with_cancel_signal():
    """cancel_event 置位中断退避 → 抛 _StreamCancel。"""
    cancel_event = asyncio.Event()
    task = asyncio.ensure_future(wait_with_execution_control(3600, cancel_event=cancel_event))
    await asyncio.sleep(0)  # 让 wait 进入等待
    cancel_event.set()
    with pytest.raises(_StreamCancel):
        await asyncio.wait_for(task, timeout=1)


async def test_wait_deadline_interrupts_with_deadline_signal():
    """deadline 到期中断退避 → 抛 _DeadlineExceeded。"""
    with pytest.raises(_DeadlineExceeded):
        await asyncio.wait_for(
            wait_with_execution_control(3600, deadline=time.monotonic() + 0.02),
            timeout=1,
        )
