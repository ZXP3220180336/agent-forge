"""Reasoning 策略测试的语义执行参数构造器。"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from app.domain.reasoning.execution import (
    ContextWindowLimits,
    ExecutionLimits,
    ModelOptions,
    ReasoningRunScope,
    RecoveryBudget,
    ToolExecutionOptions,
)


def reasoning_execution_args(
    strategy: Literal["react", "planner", "reflection"],
    **values: Any,
) -> dict[str, Any]:
    """把测试场景的标量覆盖组装成生产代码使用的语义值对象。"""

    default_run_id = {
        "react": "run-react-test",
        "planner": "run-planner-test",
        "reflection": "run-reflection-test",
    }[strategy]
    run = ReasoningRunScope(
        run_id=values.pop("run_id", default_run_id),
        run_stop=values.pop("run_stop", asyncio.Event()),
        workflow_id=values.pop("workflow_id", None),
        parent_cancel_events=values.pop("parent_cancel_events", ()),
        cancel_event=values.pop("cancel_event", None),
    )
    model = ModelOptions(
        temperature=values.pop("temperature", 0.2),
        max_tokens=values.pop("max_tokens", 1024),
    )
    limits = ExecutionLimits(
        max_iterations=values.pop("max_iterations", 10),
        max_execution_time=values.pop("max_execution_time", None),
        max_same_action_turns=values.pop("max_same_action_turns", 3),
    )
    context_window = ContextWindowLimits(
        max_rounds=values.pop("max_context_rounds", None),
        max_tokens=values.pop("max_context_tokens", None),
    )
    recovery = RecoveryBudget(
        max_empty_retries=values.pop("max_empty_retries", 2),
        max_llm_fail_retries=values.pop("max_llm_fail_retries", 2),
        max_tool_protocol_retries=values.pop("max_tool_protocol_retries", 2),
        max_replan_rounds=(
            values.pop("max_replan_rounds", 2) if strategy == "planner" else None
        ),
        max_refine_rounds=(
            values.pop("max_refine_rounds", 2)
            if strategy == "reflection"
            else None
        ),
    )
    tool_execution = ToolExecutionOptions(
        timeout=values.pop("tool_timeout", None),
        max_attempts=values.pop("tool_max_retries", None),
    )
    return {
        "run": run,
        "model": model,
        "limits": limits,
        "context_window": context_window,
        "recovery": recovery,
        "tool_execution": tool_execution,
        **values,
    }
