# ============================================
# domain/agent/planner.py - Planner Agent 桥接
# ============================================
"""
Planner Agent 实现（桥接）
===========================

Plan-then-Execute 逻辑已实现在 `app/domain/reasoning/planner.py` 的
`PlannerStrategy`（可独立复用的领域推理流程：规划 → 执行 → 汇总）。
本模块的 PlannerAgent 作为 agent/ 编排层：继承 BaseAgent 生命周期，
在 _strategy_cycle 中委托 PlannerStrategy.execute()，并把策略产出
（PlannerOutcome）组装为 AgentResult（plan/steps_executed/replan_rounds/
degraded 进 metadata，供 Phase C Orchestrator 与证据链报告消费）。
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing

from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.domain.reasoning.execution import (
    ContextWindowLimits,
    ExecutionLimits,
    ModelOptions,
    ReasoningRunScope,
    RecoveryBudget,
    ToolExecutionOptions,
)
from app.domain.reasoning.planner import PlannerOutcome, PlannerStrategy
from app.shared.error_handling import ErrorHandlerRegistry

from .base import AgentResult, BaseAgent


class PlannerAgent(BaseAgent):
    """
    Planner 策略编排（桥接 PlannerStrategy 到 BaseAgent 生命周期）。

    构造参数与 ReActAgent / ReflectionAgent 对齐（context_budget /
    error_handlers / cost_limiter / cancel_event）。
    """

    def __init__(
        self,
        llm: LLMGateway,
        tools: ToolGateway,
        context_budget: ContextBudgetPort | None = None,
        error_handlers: ErrorHandlerRegistry | None = None,
        cost_limiter: CostLimiterPort | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__(llm, tools, error_handlers=error_handlers)
        self._cancel_event = cancel_event
        self._strategy = PlannerStrategy(
            llm=llm,
            tools=tools,
            context_budget=context_budget,
            error_handlers=error_handlers,
            cost_limiter=cost_limiter,
        )

    async def _strategy_cycle(
        self,
        user_input: str,
        messages: list[dict[str, str]],
    ) -> AsyncGenerator[str]:
        """Planner 主流程：委托 PlannerStrategy.execute，产出事件；结果组装为 AgentResult。"""
        ctx = self._require_context()

        stream = self._strategy.execute(
            user_input,
            messages,
            run=ReasoningRunScope(
                run_id=ctx.run_id,
                run_stop=ctx.run_stop,
                workflow_id=ctx.workflow_id,
                parent_cancel_events=ctx.parent_cancel_events,
                cancel_event=self._cancel_event,
            ),
            model=ModelOptions(
                temperature=ctx.temperature,
                max_tokens=ctx.max_tokens,
            ),
            limits=ExecutionLimits(
                max_iterations=ctx.max_iterations,
                max_execution_time=ctx.max_execution_time,
                max_same_action_turns=ctx.max_same_action_turns,
            ),
            context_window=ContextWindowLimits(
                max_rounds=ctx.max_context_rounds,
                max_tokens=ctx.max_context_tokens,
            ),
            recovery=RecoveryBudget(
                max_empty_retries=ctx.max_empty_retries,
                max_llm_fail_retries=ctx.max_llm_fail_retries,
                max_tool_protocol_retries=ctx.max_tool_protocol_retries,
                # 现有 AgentContext 字段同时承载 Reflection 修订与 Planner replan。
                max_replan_rounds=ctx.max_refine_rounds,
            ),
            tool_execution=ToolExecutionOptions(),
            stream_mode=ctx.stream_mode,
        )
        async with aclosing(stream):
            async for event in stream:
                yield event

        self._result = self._map_outcome(self._strategy.outcome)

    def _map_outcome(self, outcome: PlannerOutcome | None) -> AgentResult:
        """PlannerOutcome → AgentResult（plan/steps_executed/replan_rounds/degraded 进 metadata）。"""
        if outcome is None:
            return AgentResult(
                success=False,
                content="",
                error="Planner 策略未产出结果",
            )
        return AgentResult(
            success=outcome.success,
            content=outcome.content,
            reasoning=outcome.reasoning,
            structured=outcome.structured,
            tool_calls=outcome.tool_calls,
            iterations=outcome.iterations,
            total_tokens=outcome.total_tokens,
            usage=outcome.usage,
            error=outcome.error,
            metadata={
                "plan": outcome.plan,
                "steps_executed": outcome.steps_executed,
                "replan_rounds": outcome.replan_rounds,
                "degraded": outcome.degraded,
            },
        )
