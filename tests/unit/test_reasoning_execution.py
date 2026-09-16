"""Reasoning 执行参数值对象契约测试。"""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from app.domain.reasoning.execution import (
    ExecutionLimits,
    ReasoningRunScope,
    RecoveryBudget,
)
from app.domain.reasoning.planner import PlannerStrategy
from app.domain.reasoning.reflection import ReflectionStrategy
from tests.reasoning_execution import reasoning_execution_args


def test_execution_values_are_frozen() -> None:
    limits = ExecutionLimits()

    with pytest.raises(FrozenInstanceError):
        limits.max_iterations = 20  # type: ignore[misc]


def test_run_scope_freezes_event_reference_not_event_state() -> None:
    run_stop = asyncio.Event()
    scope = ReasoningRunScope(run_id="run-1", run_stop=run_stop)

    run_stop.set()

    assert scope.run_stop is run_stop
    assert scope.run_stop.is_set()


@pytest.mark.parametrize(
    ("overrides", "error_type", "message"),
    [
        ({"run_id": "  "}, ValueError, "run_id"),
        ({"run_stop": object()}, TypeError, "run_stop"),
        ({"workflow_id": "  "}, ValueError, "workflow_id"),
        ({"parent_cancel_events": [asyncio.Event()]}, TypeError, "parent_cancel_events"),
        ({"cancel_event": object()}, TypeError, "cancel_event"),
    ],
)
def test_run_scope_rejects_invalid_control_identity(
    overrides: dict,
    error_type: type[Exception],
    message: str,
) -> None:
    values = {
        "run_id": "run-1",
        "run_stop": asyncio.Event(),
        **overrides,
    }

    with pytest.raises(error_type, match=message):
        ReasoningRunScope(**values)  # type: ignore[arg-type]


def test_recovery_budget_distinguishes_not_applicable_from_zero() -> None:
    common_only = RecoveryBudget()
    planner_disabled = RecoveryBudget(max_replan_rounds=0)

    assert common_only.max_replan_rounds is None
    assert planner_disabled.max_replan_rounds == 0


@pytest.mark.parametrize(
    "field_name",
    [
        "max_empty_retries",
        "max_llm_fail_retries",
        "max_tool_protocol_retries",
        "max_replan_rounds",
        "max_refine_rounds",
    ],
)
def test_recovery_budget_rejects_negative_values(field_name: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        RecoveryBudget(**{field_name: -1})


@pytest.mark.asyncio
async def test_planner_requires_replan_budget() -> None:
    strategy = PlannerStrategy(llm=None, tools=None)
    stream = strategy.execute(
        "test",
        [],
        **reasoning_execution_args("react"),
    )

    with pytest.raises(ValueError, match="max_replan_rounds"):
        await anext(stream)


@pytest.mark.asyncio
async def test_reflection_requires_refine_budget() -> None:
    strategy = ReflectionStrategy(llm=None, tools=None)
    stream = strategy.execute(
        "test",
        [],
        **reasoning_execution_args("react"),
    )

    with pytest.raises(ValueError, match="max_refine_rounds"):
        await anext(stream)
