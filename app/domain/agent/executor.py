# ============================================
# domain/agent/executor.py - ReAct Agent 桥接
# ============================================
"""
ReAct Agent 实现（桥接）
=======================

ReAct 循环逻辑已抽离到 `app/domain/reasoning/react.py` 的 ReActStrategy（领域推理流程）。
本模块的 ReActAgent 作为 agent/ 桥接类型：继承 BaseAgent 生命周期（run()/状态/事件路由），
在 _strategy_cycle 中委托 ReActStrategy.execute()，并把策略产出（ReActOutcome）组装为 AgentResult。

事件流设计（由 ReActStrategy 产出）：
    LLM 原始流 → type=reasoning（逐 token）
               → type=message（逐 token）
    Agent 发现 tool_calls → type=tool_call
    执行工具            → type=tool_result
    LLM 下一轮原始流     → type=reasoning / message
    Agent 完成           → type=done

每次 run() 是独立的，上下文通过 AgentContext 传入。
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
from app.domain.reasoning.react import ReActOutcome, ReActStrategy
from app.shared.error_handling import ErrorHandlerRegistry

from .base import AgentResult, BaseAgent


class ReActAgent(BaseAgent):
    """
    ReAct 流程桥接（把 ReActStrategy 接入 BaseAgent 生命周期）。

    对外 API（run / result / state）与事件流与抽离前一致。
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
        # 优雅取消信号（/chat/stop 经 TaskService 置位；None=不启用）
        self._cancel_event = cancel_event
        self._strategy = ReActStrategy(
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
        """ReAct 主循环：委托 ReActStrategy.execute，产出事件；结果组装为 AgentResult。"""
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
                batch_cleanup_grace=ctx.batch_cleanup_grace,
            ),
            context_window=ContextWindowLimits(
                max_rounds=ctx.max_context_rounds,
                max_tokens=ctx.max_context_tokens,
            ),
            recovery=RecoveryBudget(
                max_empty_retries=ctx.max_empty_retries,
                max_llm_fail_retries=ctx.max_llm_fail_retries,
                max_tool_protocol_retries=ctx.max_tool_protocol_retries,
            ),
            tool_execution=ToolExecutionOptions(),
            stream_mode=ctx.stream_mode,
        )
        async with aclosing(stream):
            async for event in stream:
                yield event

        self._result = self._map_outcome(self._strategy.outcome)

    def _map_outcome(self, outcome: ReActOutcome | None) -> AgentResult:
        """ReActOutcome → AgentResult。"""
        if outcome is None:
            return AgentResult(
                success=False,
                content="",
                error="ReAct 策略未产出结果",
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
        )
