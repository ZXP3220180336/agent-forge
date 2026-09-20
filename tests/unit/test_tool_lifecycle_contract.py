import asyncio
import time
from dataclasses import FrozenInstanceError

import pytest

from app.domain.ports.tool_execution import (
    ToolCallContext,
    ToolCleanupState,
    ToolEffectState,
    ToolExecutionState,
    ToolFact,
)
from app.domain.ports.tool_gateway import ErrorCode, ToolResult
from app.domain.reasoning.tool_batch import ToolBatchCollector, ToolBatchRunner
from app.integration.tools.tool_service import ToolService
from app.shared.exceptions import (
    AppErrorCode,
    NonRetryableError,
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)


def _context(**overrides) -> ToolCallContext:
    values = {
        "run_id": "run-1",
        "batch_id": "batch-1",
        "tool_call_id": "call-1",
        "operation_id": "operation-1",
        "cancel_events": (asyncio.Event(),),
        "run_stop": asyncio.Event(),
    }
    values.update(overrides)
    return ToolCallContext(**values)


def _fact(*, revision: int, result: ToolResult | None = None) -> ToolFact:
    return ToolFact(
        operation_id="operation-1",
        attempt_id="attempt-1",
        run_id="run-1",
        batch_id="batch-1",
        tool_call_id="call-1",
        revision=revision,
        execution_state=ToolExecutionState.RUNNING,
        effect_state=ToolEffectState.UNKNOWN,
        cleanup_state=ToolCleanupState.PENDING,
        result=result,
    )


def test_tool_call_context_rejects_missing_identity_and_is_frozen():
    with pytest.raises(ValueError):
        _context(run_id=" ")
    context = _context(deadline=0.0)
    with pytest.raises(FrozenInstanceError):
        context.run_id = "other"  # type: ignore[misc]


def test_tool_result_effect_defaults_unknown_and_timeout_remains_result_code():
    result = ToolResult(False, "", error_code=ErrorCode.TIMEOUT)
    assert result.effect_state == ToolEffectState.UNKNOWN
    assert result.error_code == ErrorCode.TIMEOUT


def test_collector_owns_both_recorded_and_returned_snapshots():
    source = ToolResult(True, "ok", metadata={"items": [1]})
    collector = ToolBatchCollector()
    collector.record(_fact(revision=1, result=source))

    source.metadata["items"].append(2)  # type: ignore[index]
    first = collector.snapshot()[0]
    assert first.result.metadata == {"items": [1]}

    first.result.metadata["items"].append(3)  # type: ignore[union-attr,index]
    assert collector.snapshot()[0].result.metadata == {"items": [1]}


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        (ToolCancelledError, AppErrorCode.TOOL_CANCELLED),
        (ToolDeadlineExceededError, AppErrorCode.TOOL_DEADLINE),
        (ToolRunStoppedError, AppErrorCode.TOOL_RUN_STOPPED),
    ],
)
def test_tool_control_errors_are_shared_non_retryable_types(error_type, code):
    error = error_type("stop", run_id="run-1", operation_id="operation-1", diagnostic_id="d-1")
    assert isinstance(error, NonRetryableError)
    assert error.code == code
    assert (error.run_id, error.operation_id, error.diagnostic_id) == (
        "run-1",
        "operation-1",
        "d-1",
    )


def test_collector_ignores_stale_revision_and_updates_after_close():
    collector = ToolBatchCollector()
    collector.record(_fact(revision=2))
    collector.record(_fact(revision=1, result=ToolResult(True, "stale")))
    collector.close()
    collector.record(_fact(revision=3, result=ToolResult(True, "late")))
    assert collector.snapshot()[0].revision == 2


@pytest.mark.asyncio
async def test_gateway_requires_context_and_cancel_publishes_fact_before_raise():
    service = ToolService()
    with pytest.raises(TypeError):
        await service.execute("missing", {})

    cancelled = asyncio.Event()
    cancelled.set()
    call = _context(cancel_events=(cancelled,))
    collector = ToolBatchCollector()
    with pytest.raises(ToolCancelledError):
        await service.execute("missing", {}, call=call, facts=collector)
    fact = collector.snapshot()[0]
    assert fact.execution_state == ToolExecutionState.NOT_STARTED
    assert fact.effect_state == ToolEffectState.NONE


@pytest.mark.parametrize(
    ("cancelled", "expired", "stopped", "expected"),
    [
        (True, True, True, ToolCancelledError),
        (False, True, True, ToolDeadlineExceededError),
        (False, False, True, ToolRunStoppedError),
    ],
)
@pytest.mark.asyncio
async def test_control_check_order_is_cancel_then_deadline_then_run_stop(cancelled, expired, stopped, expected):
    cancel_event = asyncio.Event()
    run_stop = asyncio.Event()
    if cancelled:
        cancel_event.set()
    if stopped:
        run_stop.set()
    call = _context(
        cancel_events=(cancel_event,),
        deadline=0.0 if expired else None,
        run_stop=run_stop,
    )
    collector = ToolBatchCollector()

    with pytest.raises(expected):
        await ToolService().execute("missing", {}, call=call, facts=collector)

    assert collector.snapshot()[0].execution_state == ToolExecutionState.NOT_STARTED


@pytest.mark.asyncio
async def test_fact_sink_programming_error_closes_run_admission():
    class _BrokenSink:
        def record(self, fact):
            raise RuntimeError("sink broken")

    service = ToolService()
    call = _context()
    with pytest.raises(RuntimeError, match="sink broken"):
        await service.execute("missing", {}, call=call, facts=_BrokenSink())
    assert call.run_stop.is_set()


@pytest.mark.asyncio
async def test_grace_exhausted_poll_captures_cooperative_cancel_only():
    """宽限耗尽后的一次即时轮询只捕获合作取消的任务。

    合作取消（`CancelledError` 直接传播）会被这次 `wait(timeout=0)` 捕获，异常写入
    `outcomes`；吞掉取消的任务留在 `pending`，`outcomes` 保持 `None`。两类在 ReAct 都走
    事实分支生成回执，故回执口径不受此差异影响——本测试锁定的是 outcome 的分野本身。
    """
    runner = ToolBatchRunner(ToolBatchCollector())

    async def execute(index: int):
        if index == 0:
            await asyncio.sleep(0.02)
            raise ToolCancelledError("stopped", run_id="run-1", operation_id="operation-1")
        if index == 1:
            await asyncio.sleep(30)  # 合作取消：CancelledError 直接写入 outcomes
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # 吞掉取消：即时轮询捕获不到，任务留在 pending
            raise

    calls = [
        _context(),
        _context(tool_call_id="call-2", operation_id="operation-2"),
        _context(tool_call_id="call-3", operation_id="operation-3"),
    ]
    outcomes = await runner.run(calls, execute)

    assert isinstance(outcomes[0], ToolCancelledError)
    assert isinstance(outcomes[1], asyncio.CancelledError)
    assert outcomes[2] is None
    await asyncio.sleep(0.1)  # 让吞掉取消的任务自然收尾，不留 pending 任务


@pytest.mark.asyncio
async def test_batch_cleanup_grace_is_taken_from_the_caller():
    """收尾窗口由调用方注入：给 0.15 秒时整批在约 0.15 秒内收尾，而非模块默认的 1 秒。

    窗口此前是硬编码常量，改配置不影响它；本测试锁定「注入值真的生效」，取样默认值会
    明显超出 0.6 秒上界。
    """
    runner = ToolBatchRunner(ToolBatchCollector())
    started = time.monotonic()

    async def execute(index: int):
        if index == 0:
            await asyncio.sleep(0.02)
            raise ToolCancelledError("stopped", run_id="run-1", operation_id="operation-1")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)  # 吞掉取消：留到收尾窗口耗尽
            raise

    outcomes = await runner.run(
        [_context(), _context(tool_call_id="call-2", operation_id="operation-2")],
        execute,
        batch_cleanup_grace=0.15,
    )
    elapsed = time.monotonic() - started

    assert isinstance(outcomes[0], ToolCancelledError)
    assert outcomes[1] is None
    assert elapsed < 0.6
    await asyncio.sleep(0.05)  # 让吞掉取消的任务自然收尾


@pytest.mark.asyncio
async def test_hard_cancel_reuses_grace_started_by_control_error():
    """控制异常已启动宽限后再遭硬取消，沿用剩余宽限，不重取一份。

    重设会让收尾窗口翻倍：宽限 1.0，控制异常在 t 起算，t+0.35 硬取消。
    沿用剩余 → 约 t+1.0 返回；重设 → 约 t+1.35 返回。
    """
    runner = ToolBatchRunner(ToolBatchCollector())
    in_flight = asyncio.Event()
    error_at: float | None = None

    async def execute(index: int):
        nonlocal error_at
        if index == 0:
            await asyncio.sleep(0.05)
            error_at = time.monotonic()
            raise ToolCancelledError("stopped", run_id="run-1", operation_id="operation-1")
        in_flight.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(30)  # 吞掉取消：真实执行未停，任务留在 pending
            raise

    calls = [_context(), _context(tool_call_id="call-2", operation_id="operation-2")]
    task = asyncio.create_task(runner.run(calls, execute))
    await in_flight.wait()
    while error_at is None:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.35)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - error_at < 1.25
