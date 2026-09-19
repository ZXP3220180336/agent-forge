"""真实线程和吞取消任务的 Owner 验收。"""

import asyncio
import threading
import time
from dataclasses import replace

import pytest

from app.domain.ports.tool_execution import ToolCallContext
from app.integration.tools.admission import ToolAdmission
from app.integration.tools.execution import (
    ToolAttemptTimeoutError,
    ToolExecutionSettings,
    ToolExecutionSupervisor,
    ToolShutdownIncompleteError,
)
from app.shared.exceptions import ToolCancelledError, ToolDeadlineExceededError


def context():
    return ToolCallContext(
        run_id="run",
        batch_id="batch",
        tool_call_id="call",
        operation_id="op",
        cancel_events=(asyncio.Event(),),
        run_stop=asyncio.Event(),
    )


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("signal", ["cancel", "deadline"])
async def test_late_value_is_owned_before_control_propagation(signal):
    call = context()
    if signal == "deadline":
        call = replace(call, deadline=time.monotonic() + 0.02)
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor()
    started = asyncio.Event()
    received = []

    async def factory(handle):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "late result"

    handle = supervisor.start(
        factory, call=call, permit=await admission.acquire(call), on_complete=lambda item: received.append(item.value)
    )
    waiting = asyncio.create_task(supervisor.wait(handle))
    await started.wait()
    if signal == "cancel":
        call.cancel_events[0].set()
    with pytest.raises(ToolCancelledError if signal == "cancel" else ToolDeadlineExceededError):
        await waiting
    assert received == ["late result"]
    assert admission.active == 0
    await supervisor.close()


async def test_local_timeout_preserves_late_value_and_natural_timeout_identity():
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor()
    received = []

    async def late(handle):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "finished during cleanup"

    call = context()
    handle = supervisor.start(
        late, call=call, permit=await admission.acquire(call), on_complete=lambda item: received.append(item.value)
    )
    with pytest.raises(ToolAttemptTimeoutError):
        await supervisor.wait(handle, timeout=0.01)
    assert received == ["finished during cleanup"]
    original = TimeoutError("provider timeout")

    async def natural(handle):
        raise original

    handle = supervisor.start(natural, call=call, permit=await admission.acquire(call))
    with pytest.raises(TimeoutError) as raised:
        await supervisor.wait(handle)
    assert raised.value is original
    await supervisor.close()


async def test_second_hard_cancel_transfers_before_propagating():
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor()
    started = asyncio.Event()
    cleaning = asyncio.Event()
    finish = asyncio.Event()

    async def late(handle):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await finish.wait()
            return "late"

    call = context()
    handle = supervisor.start(late, call=call, permit=await admission.acquire(call))
    waiting = asyncio.create_task(supervisor.wait(handle))
    await started.wait()
    waiting.cancel()
    await cleaning.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert handle.transferred and admission.active == 1
    finish.set()
    await wait_until(lambda: handle.completed)
    assert admission.active == 0
    await supervisor.close()


async def test_thread_keeps_permit_after_cancel_and_close_refuses_live_dependency():
    call = context()
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor(
        ToolExecutionSettings(cleanup_timeout_seconds=0.01, shutdown_timeout_seconds=0.01)
    )
    started = threading.Event()
    finish = threading.Event()
    owner = object()
    transfers = []

    def work():
        started.set()
        finish.wait(2)
        return "thread result"

    async def factory(handle):
        return await handle.run_sync(work)

    handle = supervisor.start(
        factory,
        call=call,
        owner=owner,
        permit=await admission.acquire(call),
        on_transfer=lambda item: transfers.append(item.transferred),
    )
    waiting = asyncio.create_task(supervisor.wait(handle))
    try:
        await wait_until(started.is_set)
        call.cancel_events[0].set()
        with pytest.raises(ToolCancelledError):
            await waiting
        assert admission.active == 1
        assert supervisor.owns(owner)
        assert transfers == [True]
        assert handle._on_complete is handle._on_transfer is None
        with pytest.raises(ToolShutdownIncompleteError):
            await supervisor.close()
    finally:
        finish.set()
        await wait_until(lambda: handle.completed)
        await supervisor.close()
    assert admission.active == 0
    assert handle.thread_results == ["thread result"]
    assert not supervisor.owns(owner)


async def test_tracking_capacity_reserved_before_start():
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor(ToolExecutionSettings(max_recovery_records=1))
    first = context()
    done = asyncio.Event()

    async def factory(handle):
        await done.wait()
        return 7

    handle = supervisor.start(factory, call=first, permit=await admission.acquire(first))
    second = replace(first, operation_id="second")
    permit = await admission.acquire(second)
    assert supervisor.start(factory, call=second, permit=permit) is None
    permit.release()
    done.set()
    assert await supervisor.wait(handle) == 7
    await supervisor.close()


async def test_completed_handle_cannot_start_new_thread_work():
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor()
    call = context()

    async def immediate(handle):
        return "finished"

    handle = supervisor.start(immediate, call=call, permit=await admission.acquire(call))
    assert await supervisor.wait(handle) == "finished"
    called = []
    try:
        with pytest.raises(RuntimeError, match="已经结束"):
            await handle.run_sync(lambda: called.append(True))
        assert called == []
    finally:
        await supervisor.close()


async def test_unacknowledged_completed_result_keeps_tracking_capacity():
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor(ToolExecutionSettings(max_recovery_records=1))
    call = context()

    async def immediate(handle):
        return "result without receiver"

    handle = supervisor.start(
        immediate,
        call=call,
        permit=await admission.acquire(call),
        on_complete=lambda item: item.retain_record(),
    )
    assert await supervisor.wait(handle) == "result without receiver"
    assert handle.completed and not handle.transferred
    assert admission.active == 0
    assert handle in supervisor.records
    second_permit = await admission.acquire(call)
    try:
        assert supervisor.start(immediate, call=call, permit=second_permit) is None
    finally:
        second_permit.release()
        await supervisor.close()


async def test_observation_without_permit_cannot_start_thread_and_tracking_is_bounded():
    supervisor = ToolExecutionSupervisor(ToolExecutionSettings(max_recovery_records=1))
    call = context()
    ready = asyncio.Event()
    finish = asyncio.Event()
    called = []

    async def observe(handle):
        with pytest.raises(RuntimeError, match="无业务 Permit"):
            await handle.run_sync(lambda: called.append(True))
        ready.set()
        await finish.wait()
        return "observed"

    handle = supervisor.start(observe, call=call, permit=None)
    await ready.wait()
    assert supervisor.start(observe, call=call, permit=None) is None
    finish.set()
    assert await supervisor.wait(handle) == "observed"
    assert called == []
    await supervisor.close()


async def test_hard_cancel_preserved_and_transfer_is_bounded():
    call = context()
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor(ToolExecutionSettings(cleanup_timeout_seconds=0.01))
    started = asyncio.Event()
    finish = asyncio.Event()

    async def factory(handle):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await finish.wait()
            return "late"

    handle = supervisor.start(factory, call=call, permit=await admission.acquire(call))
    waiting = asyncio.create_task(supervisor.wait(handle))
    await started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert handle.transferred and admission.active == 1
    finish.set()
    await wait_until(lambda: handle.completed)
    assert handle.value == "late" and admission.active == 0
    assert handle in supervisor.records
    await supervisor.close()


async def test_local_timeout_and_callback_failure_do_not_leak_permit():
    call = context()
    admission = ToolAdmission()
    supervisor = ToolExecutionSupervisor()

    async def factory(handle):
        await asyncio.Event().wait()

    handle = supervisor.start(factory, call=call, permit=await admission.acquire(call))
    with pytest.raises(TimeoutError):
        await supervisor.wait(handle, timeout=0.01)
    assert admission.active == 0

    def bad_callback(handle):
        raise ValueError("sink failed")

    async def immediate(handle):
        return "owned"

    handle = supervisor.start(immediate, call=call, permit=await admission.acquire(call), on_complete=bad_callback)
    with pytest.raises(ValueError, match="sink failed"):
        await supervisor.wait(handle)
    assert handle.value == "owned" and call.run_stop.is_set()
    assert handle in supervisor.records
    assert admission.active == 0
    await supervisor.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cleanup_timeout_seconds": float("nan")},
        {"shutdown_timeout_seconds": 0},
        {"observation_timeout_seconds": float("inf")},
        {"max_recovery_records": True},
    ],
)
def test_settings_reject_invalid_bounds(kwargs):
    with pytest.raises(ValueError):
        ToolExecutionSettings(**kwargs)
