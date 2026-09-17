"""子策略事实归属与顺序复用的回归测试。"""

import asyncio

import pytest

from tests.reasoning_execution import reasoning_execution_args

from app.domain.agent import AgentContext, PlannerAgent, ReActAgent, ReflectionAgent
from app.domain.agent.base import AgentState
from app.domain.reasoning import PlannerStrategy, ReflectionStrategy
from app.shared.exceptions import (
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)
from tests.unit.test_planner import PLAN, SUMMARY, _PlannerLLM, _stop_script
from tests.unit.test_tool_lifecycle_wiring import _FactGateway


def _tool_script(call_id):
    return {
        "finish_reason": "tool_calls",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "one", "arguments": "{}"},
            }
        ],
    }


async def _execute(strategy, run_id):
    strategy_kind = "planner" if isinstance(strategy, PlannerStrategy) else "reflection"
    return [
        event
        async for event in strategy.execute(
            "task",
            [],
            **reasoning_execution_args(
                strategy_kind,
                max_iterations=5,
                temperature=0.2,
                max_tokens=32,
                run_id=run_id,
                run_stop=asyncio.Event(),
            ),
        )
    ]


@pytest.mark.parametrize("abort_second", [False, True])
async def test_planner_keeps_earlier_and_interrupted_step_facts(abort_second):
    class Gateway(_FactGateway):
        async def execute(self, *args, **kwargs):
            if abort_second and len(self.calls) == 1:
                self.control_error = ToolCancelledError
            return await super().execute(*args, **kwargs)

    llm = _PlannerLLM(
        [
            _tool_script("first"),
            _stop_script("step one"),
            _tool_script("second"),
            _stop_script("step two"),
        ],
        [PLAN, SUMMARY],
    )
    gateway = Gateway()
    strategy = PlannerStrategy(llm=llm, tools=gateway)
    if abort_second:
        with pytest.raises(ToolCancelledError):
            await _execute(strategy, "run")
    else:
        await _execute(strategy, "run")
    assert {fact.tool_call_id for fact in strategy.tool_facts} == {"first", "second"}
    returned = next(fact for fact in strategy.tool_facts if fact.result is not None)
    returned.result.content = "mutated"
    assert all(fact.result is None or fact.result.content != "mutated" for fact in strategy.tool_facts)


@pytest.mark.parametrize("strategy_type", [PlannerStrategy, ReflectionStrategy])
async def test_parent_strategy_new_run_discards_old_outcome_and_keeps_abort_facts(
    strategy_type,
):
    llm = _PlannerLLM(
        [_stop_script("old"), _stop_script("old"), _tool_script("new")],
        [PLAN, SUMMARY, PLAN],
    )
    gateway = _FactGateway()
    strategy = strategy_type(llm=llm, tools=gateway)
    await _execute(strategy, "old-run")
    assert strategy.outcome is not None
    llm.react_scripts = [_tool_script("new")]
    gateway.control_error = ToolCancelledError
    with pytest.raises(ToolCancelledError):
        await _execute(strategy, "new-run")
    assert strategy.outcome is None
    assert {fact.run_id for fact in strategy.tool_facts} == {"new-run"}


@pytest.mark.parametrize(
    "error_type, expected_state",
    [
        (ToolCancelledError, AgentState.CANCELLED),
        (ToolDeadlineExceededError, AgentState.FAILED),
        (ToolRunStoppedError, AgentState.FAILED),
    ],
)
async def test_agent_new_run_discards_prior_success_on_tool_control(
    error_type,
    expected_state,
):
    llm = _PlannerLLM([_stop_script("old"), _tool_script("new")], [])
    gateway = _FactGateway(control_error=error_type)
    agent = ReActAgent(llm=llm, tools=gateway)

    async def run(run_id):
        context = AgentContext(session_id="s", user_id="u", run_id=run_id, run_stop=asyncio.Event())
        return [event async for event in agent.run("task", [], context)]

    await run("old-run")
    assert agent.result is not None and agent.result.success
    with pytest.raises(error_type):
        await run("new-run")
    assert agent.result is None
    assert agent._strategy.outcome is None
    assert agent.state == expected_state


@pytest.mark.parametrize("agent_type", [ReActAgent, PlannerAgent, ReflectionAgent])
async def test_agent_close_waits_for_strategy_cleanup_before_releasing_context(
    agent_type,
):
    agent = agent_type(llm=None, tools=None)
    closed = []

    async def strategy(*args, **kwargs):
        try:
            yield "strategy-event"
        finally:
            closed.append(agent._context.run_id)

    agent._strategy.execute = strategy
    context = AgentContext(session_id="s", user_id="u", run_id="run", run_stop=asyncio.Event())
    stream = agent.run("task", [], context)
    await anext(stream)
    assert await anext(stream) == "strategy-event"
    await stream.aclose()
    assert closed == ["run"]
    assert agent._context is None
    assert not agent._running


@pytest.mark.parametrize("mode", ["planner-step", "planner-fallback", "reflection"])
async def test_parent_close_takes_child_facts_before_releasing_running_flag(mode):
    llm = _PlannerLLM([_tool_script("call")], [None if mode == "planner-fallback" else PLAN])
    strategy_type = ReflectionStrategy if mode == "reflection" else PlannerStrategy
    strategy = strategy_type(llm=llm, tools=_FactGateway())
    strategy_kind = "reflection" if mode == "reflection" else "planner"
    stream = strategy.execute(
        "task",
        [],
        **reasoning_execution_args(
            strategy_kind,
            max_iterations=3,
            temperature=0.2,
            max_tokens=32,
            run_id="run",
            run_stop=asyncio.Event(),
        ),
    )
    async for event in stream:
        if '"type": "tool_result"' in event:
            break
    else:
        pytest.fail("未执行到工具结果事件")
    await stream.aclose()
    assert {fact.tool_call_id for fact in strategy.tool_facts} == {"call"}
    assert not strategy._execution_running
    assert not strategy._react._execution_running
