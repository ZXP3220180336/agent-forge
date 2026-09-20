import asyncio
import time

import pytest

from app.domain.agent import AgentContext, PlannerAgent, ReActAgent, ReflectionAgent
from app.domain.ports.llm_gateway import StreamResult
from app.domain.ports.tool_execution import (
    ToolCleanupState,
    ToolEffectState,
    ToolExecutionState,
    ToolFact,
)
from app.domain.ports.tool_gateway import ToolResult
from app.domain.reasoning import ReActStrategy
from app.integration.tools.base import BaseTool
from app.integration.tools.execution import ToolEffectClass, ToolExecutionSpec
from app.integration.tools.tool_service import ToolService
from app.shared.exceptions import (
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)
from tests.reasoning_execution import reasoning_execution_args, reasoning_run_scope


class _BlockingLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def async_generate(self, *args, result: StreamResult, **kwargs):
        self.started.set()
        await self.release.wait()
        result.content = "done"
        result.finish_reason = "stop"
        if False:
            yield ""


async def _consume(stream) -> list[str]:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_strategy_and_agent_reject_concurrent_instance_reuse():
    llm = _BlockingLLM()
    strategy = ReActStrategy(llm=llm, tools=None)
    first = asyncio.create_task(
        _consume(
            strategy.execute(
                "x",
                [],
                **reasoning_execution_args(
                    "react", max_iterations=1, temperature=0.2, max_tokens=32, run_id="run-1", run_stop=asyncio.Event()
                ),
            )
        )
    )
    await llm.started.wait()
    with pytest.raises(RuntimeError, match="不能并发执行"):
        await _consume(
            strategy.execute(
                "y",
                [],
                **reasoning_execution_args(
                    "react", max_iterations=1, temperature=0.2, max_tokens=32, run_id="run-2", run_stop=asyncio.Event()
                ),
            )
        )
    llm.release.set()
    await first

    second_llm = _BlockingLLM()
    agent = ReActAgent(llm=second_llm, tools=None)
    ctx = AgentContext(
        session_id="s",
        user_id="u",
        run_id="run-agent-1",
        run_stop=asyncio.Event(),
    )
    agent_first = asyncio.create_task(_consume(agent.run("x", [], ctx)))
    await second_llm.started.wait()
    with pytest.raises(RuntimeError, match="不能并发运行"):
        await _consume(
            agent.run(
                "y",
                [],
                AgentContext(
                    session_id="s",
                    user_id="u",
                    run_id="run-agent-2",
                    run_stop=asyncio.Event(),
                ),
            )
        )
    second_llm.release.set()
    await agent_first


@pytest.mark.parametrize("agent_type", [PlannerAgent, ReflectionAgent])
@pytest.mark.asyncio
async def test_nested_strategy_inherits_parent_run_controls(agent_type):
    agent = agent_type(llm=_BlockingLLM(), tools=None)
    captured = {}

    async def _recording_execute(*args, **kwargs):
        captured.update(kwargs)
        if False:
            yield ""

    agent._strategy.execute = _recording_execute
    run_stop = asyncio.Event()
    parent_cancel = asyncio.Event()
    ctx = AgentContext(
        session_id="s",
        user_id="u",
        run_id="parent-run",
        run_stop=run_stop,
        workflow_id="workflow-1",
        parent_cancel_events=(parent_cancel,),
    )
    await _consume(agent.run("x", [], ctx))
    run = captured["run"]
    assert run.run_id == "parent-run"
    assert run.run_stop is run_stop
    assert run.workflow_id == "workflow-1"
    assert run.parent_cancel_events == (parent_cancel,)


class _FactGateway:
    def __init__(self, *, control_error=None) -> None:
        self.calls = []
        self.control_error = control_error

    def get_openai_tools(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "one",
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    async def execute(self, name, parameters, *args, call, facts, **kwargs):
        self.calls.append(call)
        result = ToolResult(success=True, content=name, effect_state=ToolEffectState.NONE)
        facts.record(
            ToolFact(
                operation_id=call.operation_id,
                attempt_id="attempt-1",
                run_id=call.run_id,
                batch_id=call.batch_id,
                tool_call_id=call.tool_call_id,
                revision=1,
                execution_state=ToolExecutionState.SUCCEEDED,
                effect_state=ToolEffectState.NONE,
                cleanup_state=ToolCleanupState.COMPLETE,
                result=result,
            )
        )
        if self.control_error is not None:
            raise self.control_error("cancelled", run_id=call.run_id, operation_id=call.operation_id)
        return result


def _calls():
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        for call_id, name in (("call-1", "one"), ("call-2", "two"))
    ]


@pytest.mark.asyncio
async def test_react_assigns_batch_call_operation_and_collects_independent_facts():
    gateway = _FactGateway()
    strategy = ReActStrategy(llm=_BlockingLLM(), tools=gateway)
    messages = []
    await _consume(
        strategy.execute_tool_calls(
            _calls(),
            messages,
            1,
            run=reasoning_run_scope("run-1"),
            deadline=12.0,
            cleanup_deadline=13.0,
        )
    )
    assert {call.tool_call_id for call in gateway.calls} == {"call-1", "call-2"}
    assert len({call.batch_id for call in gateway.calls}) == 1
    assert len({call.operation_id for call in gateway.calls}) == 2
    assert all(call.deadline == 12.0 for call in gateway.calls)
    assert all(call.cleanup_deadline == 13.0 for call in gateway.calls)
    assert {fact.tool_call_id for fact in strategy.tool_facts} == {"call-1", "call-2"}
    returned = next(fact for fact in strategy.tool_facts if fact.result is not None)
    returned.result.content = "mutated"
    assert all(fact.result is None or fact.result.content != "mutated" for fact in strategy.tool_facts)


@pytest.mark.parametrize(
    "error_type",
    [ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError],
)
@pytest.mark.asyncio
async def test_react_collects_fact_before_control_exception_propagates(error_type):
    gateway = _FactGateway(control_error=error_type)
    strategy = ReActStrategy(llm=_BlockingLLM(), tools=gateway)
    with pytest.raises(error_type):
        await _consume(
            strategy.execute_tool_calls(
                [_calls()[0]],
                [],
                1,
                run=reasoning_run_scope("run-1"),
            )
        )
    assert any(fact.execution_state == ToolExecutionState.SUCCEEDED for fact in strategy.tool_facts)


@pytest.mark.asyncio
async def test_control_error_masks_unexpected_error_in_same_batch():
    """同批既有控制异常又有意外异常时上抛控制异常，且两条调用都留下回执。

    控制异常是更准确的归因，意外异常被有意掩盖（见 `_handle_tool_calls` 末尾的固定优先级）；
    掩盖不等于丢弃——两条调用的 tool 回执仍按输入顺序写进历史。
    """

    class _MixedGateway(_FactGateway):
        async def execute(self, name, parameters, *args, call, facts, **kwargs):
            if name == "two":
                raise ToolCancelledError("stopped", run_id=call.run_id, operation_id=call.operation_id)
            raise RuntimeError("工具内部意外")

    strategy = ReActStrategy(llm=_BlockingLLM(), tools=_MixedGateway())
    messages = []
    with pytest.raises(ToolCancelledError):
        await _consume(strategy.execute_tool_calls(_calls(), messages, 1, run=reasoning_run_scope("run-1")))

    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == [
        "call-1",
        "call-2",
    ]


class _PartialBatchGateway(_FactGateway):
    def __init__(self, error_type=ToolCancelledError) -> None:
        super().__init__()
        self.first_finished = asyncio.Event()
        self.error_type = error_type

    async def execute(self, name, parameters, *args, call, facts, **kwargs):
        if name == "two":
            await self.first_finished.wait()
            raise self.error_type("stopped", run_id=call.run_id, operation_id=call.operation_id)
        result = await super().execute(name, parameters, *args, call=call, facts=facts, **kwargs)
        self.first_finished.set()
        return result


@pytest.mark.parametrize("error_type", [ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError])
@pytest.mark.asyncio
async def test_partial_batch_commits_finished_sibling_and_pairs_all_protocol_calls(error_type):
    gateway = _PartialBatchGateway(error_type)
    strategy = ReActStrategy(llm=_BlockingLLM(), tools=gateway)
    messages = [{"role": "assistant", "content": "", "tool_calls": _calls()}]
    with pytest.raises(error_type):
        await _consume(strategy.execute_tool_calls(_calls(), messages, 1, run=reasoning_run_scope("run-1")))

    assert [record["tool"] for record in strategy._tool_call_records] == ["one", "two"]
    assert strategy._tool_call_records[0]["success"] is True
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == ["call-1", "call-2"]
    assert any(fact.result and fact.result.content == "one" for fact in strategy.tool_facts)


@pytest.mark.asyncio
async def test_transferred_sibling_gets_unknown_receipt_without_waiting_for_late_work():
    released = asyncio.Event()
    second_started = asyncio.Event()

    class _PendingGateway(_FactGateway):
        async def execute(self, name, parameters, *args, call, facts, **kwargs):
            if name == "two":
                facts.record(
                    ToolFact(
                        operation_id=call.operation_id,
                        run_id=call.run_id,
                        batch_id=call.batch_id,
                        tool_call_id=call.tool_call_id,
                        revision=1,
                        execution_state=ToolExecutionState.RUNNING,
                        effect_state=ToolEffectState.UNKNOWN,
                        cleanup_state=ToolCleanupState.PENDING,
                    )
                )
                second_started.set()
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    await released.wait()
                return ToolResult(True, "late")
            await second_started.wait()
            await super().execute(name, parameters, *args, call=call, facts=facts, **kwargs)
            raise ToolCancelledError("cancelled", run_id=call.run_id, operation_id=call.operation_id)

    strategy = ReActStrategy(llm=_BlockingLLM(), tools=_PendingGateway())
    messages = []
    try:
        with pytest.raises(ToolCancelledError):
            await _consume(
                strategy.execute_tool_calls(
                    _calls(),
                    messages,
                    1,
                    run=reasoning_run_scope("run-1"),
                    cleanup_deadline=time.monotonic() + 0.05,
                )
            )
        assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == ["call-1", "call-2"]
        assert "尚未确认" in messages[-1]["content"]
        assert strategy._tool_call_records[0]["success"] is True
        assert strategy._tool_call_records[1]["success"] is False
    finally:
        released.set()
        await asyncio.sleep(0)


class _ToolBatchLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def async_generate(self, *args, result: StreamResult, **kwargs):
        self.calls += 1
        result.finish_reason = "tool_calls"
        result.tool_calls = _calls()
        if False:
            yield ""


@pytest.mark.asyncio
async def test_react_controlled_partial_batch_preserves_typed_exit_and_no_new_llm_call():
    llm = _ToolBatchLLM()
    gateway = _PartialBatchGateway()
    strategy = ReActStrategy(llm=llm, tools=gateway)
    messages = [{"role": "user", "content": "x"}]
    events = []
    with pytest.raises(ToolCancelledError):
        async for event in strategy.execute(
            "x", messages, **reasoning_execution_args("react", max_iterations=3, run_id="run-1")
        ):
            events.append(event)

    assert llm.calls == 1
    assert sum('"type": "done"' in event for event in events) == 0
    assert strategy.outcome is None
    assert len(strategy._tool_call_records) == 2
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == ["call-1", "call-2"]


@pytest.mark.asyncio
async def test_real_tool_service_parallel_cancel_keeps_completed_sibling_and_valid_history():
    cancelled = asyncio.Event()
    second_started = asyncio.Event()

    class _ReadTool(BaseTool):
        def __init__(self, name: str) -> None:
            self._name = name
            self.calls = 0

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return "read"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
            return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

        async def execute(self, **kwargs) -> ToolResult:
            self.calls += 1
            if self.name == "two":
                second_started.set()
                await asyncio.Event().wait()
            else:
                await second_started.wait()
            return ToolResult(True, self.name)

    class _CancelAfterSuccess(ToolService):
        async def execute(self, name, parameters, *args, **kwargs):
            result = await super().execute(name, parameters, *args, **kwargs)
            if name == "one":
                cancelled.set()
            return result

    llm = _ToolBatchLLM()
    service = _CancelAfterSuccess(max_concurrent_tools=3)
    first, second = _ReadTool("one"), _ReadTool("two")
    service.register(first)
    service.register(second)
    strategy = ReActStrategy(llm=llm, tools=service)
    messages = [{"role": "user", "content": "x"}]

    events = []
    with pytest.raises(ToolCancelledError):
        async for event in strategy.execute(
            "x", messages, **reasoning_execution_args("react", max_iterations=3, cancel_event=cancelled)
        ):
            events.append(event)

    assert (first.calls, second.calls, llm.calls) == (1, 1, 1)
    assert sum('"type": "done"' in event for event in events) == 0
    assert [message["tool_call_id"] for message in messages if message["role"] == "tool"] == ["call-1", "call-2"]
    assert strategy.outcome is None and strategy._tool_call_records[0]["success"] is True
    assert strategy._tool_call_records[1]["success"] is False
    assert any(fact.result and fact.result.content == "one" for fact in strategy.tool_facts)
    await service.shutdown()


@pytest.mark.asyncio
async def test_tool_batch_history_is_complete_before_first_result_event_is_yielded():
    strategy = ReActStrategy(llm=_BlockingLLM(), tools=_FactGateway())
    messages = []
    stream = strategy.execute_tool_calls(_calls(), messages, 1, run=reasoning_run_scope("run-1"))

    await anext(stream)
    assert messages[0]["role"] == "assistant"
    assert [message["tool_call_id"] for message in messages[1:]] == ["call-1", "call-2"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_closing_before_tool_batch_starts_does_not_leave_unpaired_assistant_history():
    strategy = ReActStrategy(llm=_ToolBatchLLM(), tools=_FactGateway())
    messages = [{"role": "user", "content": "x"}]
    stream = strategy.execute("x", messages, **reasoning_execution_args("react", max_iterations=3))

    await anext(stream)  # 第 1 轮状态事件，此时工具批次尚未提交
    await stream.aclose()
    assert messages == [{"role": "user", "content": "x"}]


@pytest.mark.asyncio
async def test_final_answer_mixed_with_normal_tool_is_rejected_before_history_commit():
    class _MixedLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def async_generate(self, *args, result: StreamResult, **kwargs):
            self.calls += 1
            if self.calls == 1:
                result.finish_reason = "tool_calls"
                result.tool_calls = [
                    _calls()[0],
                    {
                        "id": "final",
                        "type": "function",
                        "function": {"name": "final_answer", "arguments": '{"answer":"x"}'},
                    },
                ]
            else:
                result.finish_reason = "stop"
                result.content = "after correction"
            if False:
                yield ""

    llm = _MixedLLM()
    gateway = _FactGateway()
    strategy = ReActStrategy(llm=llm, tools=gateway)
    messages = [{"role": "user", "content": "x"}]
    await _consume(
        strategy.execute(
            "x",
            messages,
            output_schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
            **reasoning_execution_args("react", max_iterations=2, max_tool_protocol_retries=1),
        )
    )
    assert llm.calls == 2
    assert gateway.calls == []
    assert all("tool_calls" not in message for message in messages)


class _DuplicateCallLLM:
    async def async_generate(self, *args, result: StreamResult, **kwargs):
        result.finish_reason = "tool_calls"
        result.tool_calls = [
            {
                "id": "duplicate",
                "type": "function",
                "function": {"name": "one", "arguments": "{}"},
            },
            {
                "id": "duplicate",
                "type": "function",
                "function": {"name": "one", "arguments": "{}"},
            },
        ]
        if False:
            yield ""


@pytest.mark.asyncio
async def test_invalid_batch_call_identity_never_reaches_gateway_or_history():
    gateway = _FactGateway()
    strategy = ReActStrategy(llm=_DuplicateCallLLM(), tools=gateway)
    messages = [{"role": "user", "content": "x"}]
    await _consume(
        strategy.execute(
            "x",
            messages,
            **reasoning_execution_args(
                "react",
                max_iterations=1,
                temperature=0.2,
                max_tokens=32,
                max_tool_protocol_retries=0,
                run_id="run-1",
                run_stop=asyncio.Event(),
            ),
        )
    )
    assert gateway.calls == []
    assert messages == [{"role": "user", "content": "x"}]
