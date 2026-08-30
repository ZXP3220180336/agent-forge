# ============================================
# domain/agent/executor.py - ReAct Agent 桥接
# ============================================
"""
ReAct Agent 实现（桥接）
=======================

ReAct 循环逻辑已抽离到 `app/domain/reasoning/react.py` 的 ReActStrategy（原子推理策略）。
本模块的 ReActAgent 作为 agent/ 编排层：继承 BaseAgent 生命周期（run()/状态/事件路由），
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

from collections.abc import AsyncGenerator

from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.domain.reasoning.react import ReActOutcome, ReActStrategy
from app.shared.error_handling import ErrorHandlerRegistry

from .base import AgentResult, BaseAgent


class ReActAgent(BaseAgent):
    """
    ReAct 策略编排（桥接 ReActStrategy 到 BaseAgent 生命周期）。

    对外 API（run / result / state）与事件流与抽离前一致；
    _execute_tool_calls 转发到策略原语，供既有测试与编排复用。
    """

    def __init__(
        self,
        llm: LLMGateway,
        tools: ToolGateway,
        context_budget: ContextBudgetPort | None = None,
        error_handlers: ErrorHandlerRegistry | None = None,
        cost_limiter: CostLimiterPort | None = None,
    ) -> None:
        super().__init__(llm, tools, error_handlers=error_handlers)
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
        ):
            yield event

        self._result = self._map_outcome(self._strategy.outcome)

    async def _execute_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        iteration: int,
    ) -> AsyncGenerator[str]:
        """工具并行执行原语转发（行为与抽离前一致；供 PlannerAgent 等复用语义参照）。"""
        async for event in self._strategy.execute_tool_calls(
            tool_calls,
            messages,
            iteration,
        ):
            yield event

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
