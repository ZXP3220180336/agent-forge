"""ToolAdmission 的共享容量、有界排队、可中断等待与撤回回归。"""

import asyncio

import pytest

from app.domain.ports.tool_execution import ToolCallContext
from app.integration.tools.admission import ToolAdmission
from app.shared.exceptions import ToolCancelledError, ToolRunStoppedError


def _call(run_id: str, suffix: str, *, cancel: asyncio.Event | None = None):
    return ToolCallContext(
        run_id=run_id,
        batch_id=f"batch-{suffix}",
        tool_call_id=f"call-{suffix}",
        operation_id=f"operation-{suffix}",
        cancel_events=(cancel or asyncio.Event(),),
        run_stop=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_global_capacity_is_shared_across_runs():
    admission = ToolAdmission(global_limit=1, per_run_limit=1)
    first = await admission.acquire(_call("run-a", "a"))
    assert first is not None

    waiting = asyncio.create_task(admission.acquire(_call("run-b", "b")))
    await asyncio.sleep(0)
    assert admission.active == 1
    assert admission.pending == 1

    first.release()
    second = await asyncio.wait_for(waiting, timeout=0.2)
    assert second is not None
    assert admission.active == 1
    second.release()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_round_robin_prevents_one_run_from_bypassing_another():
    admission = ToolAdmission(global_limit=1, per_run_limit=1)
    first = await admission.acquire(_call("run-a", "a0"))
    assert first is not None

    run_a = asyncio.create_task(admission.acquire(_call("run-a", "a1")))
    run_b = asyncio.create_task(admission.acquire(_call("run-b", "b1")))
    await asyncio.sleep(0)
    first.release()

    next_permit = await asyncio.wait_for(run_b, timeout=0.2)
    assert next_permit is not None
    assert not run_a.done()
    next_permit.release()
    queued_a = await asyncio.wait_for(run_a, timeout=0.2)
    assert queued_a is not None
    queued_a.release()


@pytest.mark.asyncio
async def test_cancel_interrupts_admission_wait_and_keeps_existing_permit():
    admission = ToolAdmission(global_limit=1, per_run_limit=1)
    first = await admission.acquire(_call("run-a", "a"))
    assert first is not None
    cancel = asyncio.Event()
    waiting = asyncio.create_task(admission.acquire(_call("run-b", "b", cancel=cancel)))
    await asyncio.sleep(0)
    cancel.set()

    with pytest.raises(ToolCancelledError):
        await asyncio.wait_for(waiting, timeout=0.2)
    assert admission.pending == 0
    assert admission.active == 1
    first.release()


@pytest.mark.asyncio
async def test_pending_capacity_returns_none_without_running_tool():
    admission = ToolAdmission(
        global_limit=1,
        per_run_limit=1,
        max_pending=1,
        max_pending_per_run=1,
        admission_timeout=0.1,
    )
    first = await admission.acquire(_call("run-a", "a"))
    assert first is not None
    waiting = asyncio.create_task(admission.acquire(_call("run-b", "b")))
    await asyncio.sleep(0)

    rejected = await admission.acquire(_call("run-c", "c"))
    assert rejected is None
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    first.release()


@pytest.mark.asyncio
async def test_admission_timeout_returns_none_without_leaking_pending_state():
    admission = ToolAdmission(
        global_limit=1,
        per_run_limit=1,
        max_pending=2,
        max_pending_per_run=2,
        admission_timeout=0.01,
    )
    first = await admission.acquire(_call("run-a", "a"))
    assert first is not None

    result = await admission.acquire(_call("run-b", "b"))
    assert result is None
    assert admission.pending == 0
    assert admission.active == 1
    first.release()


@pytest.mark.asyncio
async def test_completed_waiter_does_not_consume_capacity_or_block_next_waiter():
    admission = ToolAdmission(global_limit=1, per_run_limit=1)
    first = await admission.acquire(_call("run-a", "a"))
    assert first is not None

    stale = asyncio.create_task(admission.acquire(_call("run-b", "stale")))
    next_waiter = asyncio.create_task(admission.acquire(_call("run-c", "next")))
    await asyncio.sleep(0)

    # 模拟等待者在被 _pump 取出前已收到其他终态；该 Future 不再能接收 Permit。
    stale_future = admission._queues["run-b"][0].future
    stale_future.set_result(None)

    first.release()
    assert await asyncio.wait_for(stale, timeout=0.2) is None
    permit = await asyncio.wait_for(next_waiter, timeout=0.2)
    assert permit is not None
    assert admission.active == 1
    permit.release()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_completed_waiter_at_run_head_does_not_block_same_run_queue():
    admission = ToolAdmission(global_limit=1, per_run_limit=2)
    first = await admission.acquire(_call("run-a", "a0"))
    assert first is not None

    stale = asyncio.create_task(admission.acquire(_call("run-a", "stale")))
    next_waiter = asyncio.create_task(admission.acquire(_call("run-a", "next")))
    await asyncio.sleep(0)

    admission._queues["run-a"][0].future.set_result(None)
    first.release()

    assert await asyncio.wait_for(stale, timeout=0.2) is None
    permit = await asyncio.wait_for(next_waiter, timeout=0.2)
    assert permit is not None
    permit.release()


@pytest.mark.asyncio
async def test_close_run_withdraws_waiters_and_keeps_inflight_permit():
    """close_run：撤回该运行的排队者（写入 None），不触碰已取得 Permit 的在途调用。"""
    admission = ToolAdmission(global_limit=1, per_run_limit=1, max_pending=2, max_pending_per_run=2)
    inflight = await admission.acquire(_call("run-a", "a"))
    assert inflight is not None

    waiting = asyncio.create_task(admission.acquire(_call("run-a", "a2")))
    await asyncio.sleep(0)
    assert admission.pending == 1

    await admission.close_run("run-a")

    assert await asyncio.wait_for(waiting, timeout=0.2) is None
    assert admission.pending == 0
    assert admission.active == 1  # 在途 Permit 不受撤回影响

    inflight.release()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_close_withdraws_all_waiters_and_refuses_new_admission():
    """close：撤回全部排队者，之后新准入抛 ToolRunStoppedError（一次性单向转换）。"""
    admission = ToolAdmission(global_limit=1, per_run_limit=1, max_pending=2, max_pending_per_run=2)
    inflight = await admission.acquire(_call("run-a", "a"))
    assert inflight is not None

    waiting = asyncio.create_task(admission.acquire(_call("run-b", "b")))
    await asyncio.sleep(0)
    assert admission.pending == 1

    await admission.close()

    assert await asyncio.wait_for(waiting, timeout=0.2) is None
    assert admission.pending == 0
    assert admission.active == 1

    with pytest.raises(ToolRunStoppedError):
        await admission.acquire(_call("run-c", "c"))

    inflight.release()
    assert admission.active == 0


@pytest.mark.asyncio
async def test_cancel_racing_with_grant_does_not_leak_capacity(monkeypatch):
    """取消与放行同刻：Future 已写入 Permit 但调用方未取得时，由收尾路径代为释放。

    `_finalize_waiter` 在取消监听任务之后、移除等待者之前有一个出让点。这里在该点
    放行等待者，复现「Permit 已产生、调用方却因取消没拿到」的竞态，断言容量不泄漏。
    """
    admission = ToolAdmission(global_limit=1, per_run_limit=1)
    inflight = await admission.acquire(_call("run-a", "a"))
    assert inflight is not None

    waiting = asyncio.create_task(admission.acquire(_call("run-b", "b")))
    await asyncio.sleep(0)
    assert admission.pending == 1

    remove_waiter = ToolAdmission._remove_waiter

    def remove_after_release(self, waiter):
        inflight.release()  # 等待者仍在队列中 → _pump 给它写入 Permit
        remove_waiter(self, waiter)

    monkeypatch.setattr(ToolAdmission, "_remove_waiter", remove_after_release)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert admission.pending == 0
    assert admission.active == 0  # Permit 被代为回收，容量未泄漏
