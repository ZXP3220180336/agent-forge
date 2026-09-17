import asyncio

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
