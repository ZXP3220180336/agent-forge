"""执行阶段与事实交付责任的回归测试。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.domain.ports.tool_execution import (
    ToolCleanupState,
    ToolEffectState,
    ToolExecutionState,
)
from app.domain.ports.tool_gateway import ErrorCode, ToolResult
from app.domain.reasoning.tool_batch import ToolBatchCollector
from app.integration.tools.base import BaseTool
from app.integration.tools.execution import ToolEffectClass, ToolExecutionSpec
from app.integration.tools.tool_service import ToolService
from tests.tool_lifecycle import execution_kwargs


class _ResultTool(BaseTool):
    name = "fact_probe"
    description = "fact probe"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.VALIDATION,
        ErrorCode.REJECTED,
        ErrorCode.JSON_PARSE,
        ErrorCode.NOT_REGISTERED,
        ErrorCode.TIMEOUT,
    ],
)
async def test_returned_business_code_does_not_rewrite_execution_stage(code):
    service = ToolService()
    tool = _ResultTool(ToolResult(False, "executed", error_code=code, effect_state=ToolEffectState.NONE))
    service.register(tool)
    context = execution_kwargs()
    await service.execute(tool.name, {}, **context)

    assert tool.calls == 1
    facts = context["facts"].snapshot()
    assert len(facts) == 2
    assert all(f.execution_state == ToolExecutionState.FAILED for f in facts)
    assert all(f.cleanup_state == ToolCleanupState.COMPLETE for f in facts)
    assert all(f.effect_state == ToolEffectState.NONE for f in facts)


async def test_preflight_failure_still_records_not_started():
    context = execution_kwargs()
    await ToolService().execute("missing", {}, **context)
    (fact,) = context["facts"].snapshot()
    assert fact.execution_state == ToolExecutionState.NOT_STARTED
    assert fact.effect_state == ToolEffectState.NONE
    assert fact.cleanup_state == ToolCleanupState.NOT_NEEDED


async def test_completed_acknowledged_facts_do_not_accumulate_in_executor():
    service = ToolService()
    tool = _ResultTool(ToolResult(True, "ok"))
    service.register(tool)
    for _ in range(5):
        context = execution_kwargs()
        await service.execute(tool.name, {}, **context)
        assert len(context["facts"].snapshot()) == 2
        assert service._executor._latest_facts == {}


async def test_closed_collector_does_not_acknowledge_or_release_facts():
    service = ToolService()
    tool = _ResultTool(ToolResult(True, "ok"))
    service.register(tool)
    context = execution_kwargs()
    context["facts"].close()
    await service.execute(tool.name, {}, **context)
    assert context["facts"].snapshot() == ()
    assert len(service._executor._latest_facts) == 2
    for fact in service._executor._latest_facts.values():
        assert context["facts"].record(fact) is False


async def test_readonly_transport_timeout_has_no_effect_and_local_cleanup_is_complete():
    service = ToolService()
    tool = _ResultTool(error=TimeoutError("uncertain transport"))
    service.register(tool)
    context = execution_kwargs()
    await service.execute(tool.name, {}, **context)
    assert service._executor._latest_facts == {}
    assert all(f.cleanup_state == ToolCleanupState.COMPLETE for f in context["facts"].snapshot())
    assert all(f.effect_state == ToolEffectState.NONE for f in context["facts"].snapshot())


async def test_delivery_failure_retains_completed_fact_and_stops_run():
    class _RejectCompletion(ToolBatchCollector):
        def record(self, fact):
            if fact.execution_state == ToolExecutionState.SUCCEEDED:
                raise RuntimeError("completion delivery failed")
            return super().record(fact)

    service = ToolService()
    tool = _ResultTool(ToolResult(True, "valuable"))
    service.register(tool)
    context = execution_kwargs()
    context["facts"] = _RejectCompletion()
    with pytest.raises(RuntimeError, match="completion delivery failed"):
        await service.execute(tool.name, {}, **context)
    assert context["call"].run_stop.is_set()
    assert tool.calls == 1
    assert any(f.result and f.result.content == "valuable" for f in service._executor._latest_facts.values())


async def test_collector_acknowledges_duplicate_and_older_deliveries():
    context = execution_kwargs()
    await ToolService().execute("missing", {}, **context)
    collector = context["facts"]
    (current,) = collector.snapshot()
    assert collector.record(current) is True
    assert collector.record(replace(current, revision=current.revision - 1)) is True
    assert collector.snapshot() == (current,)


async def test_successful_retry_preserves_previous_transport_failure_in_collector():
    class _RetryTool(_ResultTool):
        async def execute(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("first attempt remains uncertain")
            return ToolResult(True, "second attempt completed")

        def can_retry(self, result_or_error):
            return True

    service = ToolService()
    tool = _RetryTool()
    service.register(tool)
    context = execution_kwargs()
    result = await service.execute(tool.name, {}, max_retries=2, retry_delay=0, **context)
    assert result.success
    assert tool.calls == 2
    previous = [f for f in context["facts"].snapshot() if f.attempt_id and f.result and not f.result.success]
    assert len(previous) == 1
    assert previous[0].execution_state == ToolExecutionState.FAILED
    assert previous[0].cleanup_state == ToolCleanupState.COMPLETE
    assert previous[0].effect_state == ToolEffectState.NONE
    assert previous[0].result.error_code == ErrorCode.TIMEOUT
    assert service._executor._latest_facts == {}


@pytest.mark.parametrize(
    "code",
    [ErrorCode.VALIDATION, ErrorCode.REJECTED, ErrorCode.JSON_PARSE, ErrorCode.NOT_REGISTERED, ErrorCode.TIMEOUT],
)
def test_attempt_result_preserves_partial_effect_regardless_of_business_code(code):
    """低层解释不把业务错误码当作未执行证据；门禁不允许此类工具正式执行。"""
    result = ToolResult(False, "executed", error_code=code, effect_state=ToolEffectState.PARTIAL)
    handle = SimpleNamespace(completed=True, error=None, value=result)
    interpreted, failure, state, cleanup, retry_allowed = ToolService()._executor._attempt_result(
        handle,
        started=True,
        timeout=5,
    )
    assert interpreted is result and failure is result
    assert state == ToolExecutionState.FAILED
    assert cleanup == ToolCleanupState.COMPLETE
    assert interpreted.effect_state == ToolEffectState.PARTIAL
    assert retry_allowed


def test_attempt_result_keeps_unknown_transport_effect_before_trusted_declaration():
    """未结合可信只读声明时，传输超时只证明本地完成，不证明远端无副作用。"""
    error = TimeoutError("uncertain transport")
    handle = SimpleNamespace(completed=True, error=error, value=None)
    result, failure, state, cleanup, retry_allowed = ToolService()._executor._attempt_result(
        handle,
        started=True,
        timeout=5,
    )
    assert failure is error
    assert result.error_code == ErrorCode.TIMEOUT
    assert state == ToolExecutionState.FAILED
    assert cleanup == ToolCleanupState.COMPLETE
    assert result.effect_state == ToolEffectState.UNKNOWN
    assert retry_allowed
