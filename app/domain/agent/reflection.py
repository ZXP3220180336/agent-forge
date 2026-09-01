# ============================================
# domain/agent/reflection.py - Reflection Agent 桥接
# ============================================
"""
Reflection Agent 实现（桥接）
============================

Reflection 三阶段逻辑已实现在 `app/domain/reasoning/reflection.py` 的
`ReflectionStrategy`（原子推理策略：生成 → 自查 → 修正）。
本模块的 ReflectionAgent 作为 agent/ 编排层：继承 BaseAgent 生命周期，
在 _strategy_cycle 中委托 ReflectionStrategy.execute()，并把策略产出
（ReflectionOutcome）组装为 AgentResult（draft/critique/refine_rounds/degraded
进 metadata，供证据链报告消费）。
"""

import asyncio
from collections.abc import AsyncGenerator

from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.domain.reasoning.reflection import (
    ReflectionOutcome,
    ReflectionStrategy,
)
from app.shared.error_handling import ErrorHandlerRegistry

from .base import AgentResult, BaseAgent


class ReflectionAgent(BaseAgent):
    """
    Reflection 策略编排（桥接 ReflectionStrategy 到 BaseAgent 生命周期）。

    构造参数与 ReActAgent 对齐（context_budget / error_handlers / cost_limiter /
    cancel_event），另可注入 output_schema / critique_schema 覆盖策略契约。
    """

    def __init__(
        self,
        llm: LLMGateway,
        tools: ToolGateway,
        context_budget: ContextBudgetPort | None = None,
        error_handlers: ErrorHandlerRegistry | None = None,
        cost_limiter: CostLimiterPort | None = None,
        cancel_event: asyncio.Event | None = None,
        output_schema: dict | None = None,
        critique_schema: dict | None = None,
    ) -> None:
        super().__init__(llm, tools, error_handlers=error_handlers)
        self._cancel_event = cancel_event
        self._strategy = ReflectionStrategy(
            llm=llm,
            tools=tools,
            context_budget=context_budget,
            error_handlers=error_handlers,
            cost_limiter=cost_limiter,
            output_schema=output_schema,
            critique_schema=critique_schema,
        )

    async def _strategy_cycle(
        self,
        user_input: str,
        messages: list[dict[str, str]],
    ) -> AsyncGenerator[str]:
        """Reflection 主流程：委托 ReflectionStrategy.execute，产出事件；结果组装为 AgentResult。"""
        ctx = self._context
        if ctx is None:
            raise RuntimeError("AgentContext 未设置")

        async for event in self._strategy.execute(
            user_input,
            messages,
            max_iterations=ctx.max_iterations,
            temperature=ctx.temperature,
            max_tokens=ctx.max_tokens,
            max_execution_time=ctx.max_execution_time,
            max_context_rounds=ctx.max_context_rounds,
            max_context_tokens=ctx.max_context_tokens,
            max_empty_retries=ctx.max_empty_retries,
            max_llm_fail_retries=ctx.max_llm_fail_retries,
            max_same_action_turns=ctx.max_same_action_turns,
            max_refine_rounds=ctx.max_refine_rounds,
            cancel_event=self._cancel_event,
        ):
            yield event

        self._result = self._map_outcome(self._strategy.outcome)

    def _map_outcome(self, outcome: ReflectionOutcome | None) -> AgentResult:
        """ReflectionOutcome → AgentResult（draft/critique/refine_rounds/degraded 进 metadata）。"""
        if outcome is None:
            return AgentResult(
                success=False,
                content="",
                error="Reflection 策略未产出结果",
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
                "draft": outcome.draft,
                "critique": outcome.critique,
                "refine_rounds": outcome.refine_rounds,
                "degraded": outcome.degraded,
            },
        )
