import asyncio
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
from app.domain.reasoning.tool_batch import ToolBatchCollector
from app.shared.exceptions import (
    AppErrorCode,
    NonRetryableError,
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)
from app.integration.tools.tool_service import ToolService


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
    error = error_type(
        "stop", run_id="run-1", operation_id="operation-1", diagnostic_id="d-1"
    )
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
async def test_control_check_order_is_cancel_then_deadline_then_run_stop(
    cancelled, expired, stopped, expected
):
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
