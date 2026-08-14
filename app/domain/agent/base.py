# ============================================
# core/agent/base.py - Agent 基类与数据定义
# ============================================
"""
Agent 基类定义
================

设计目标：
- 策略模式：BaseAgent.run() 是统一入口，_strategy_cycle() 由子类实现具体策略
- 无状态：每次 run() 新建实例，上下文通过 AgentContext 传入
- 流式友好：通过 _emit_event() 生成 SSE 事件，内层逻辑与外层输出解耦
- 可扩展：钩子方法 on_tool_call / on_thought / on_complete 供子类覆盖

支持策略（逐步实现）：
- ReAct（当前）：推理 → 工具 → 推理 → 工具 → ...
- Plan-then-Execute（预留）：先规划再批量执行
- Reflection（预留）：输出后自我反思修正
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.config import settings
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.shared.events import build_error_event, build_info_event


class AgentState(Enum):
    """Agent 状态枚举"""

    IDLE = "idle"  # 空闲，等待输入
    THINKING = "thinking"  # LLM 推理中
    WAITING = "waiting"  # 等待工具执行结果
    COMPLETED = "completed"  # 任务完成
    FAILED = "failed"  # 任务失败
    CANCELLED = "cancelled"  # 被取消


@dataclass
class AgentContext:
    """
    Agent 上下文信息（不可变，每次 run() 传入）

    所有 Agent 运行所需的外部依赖和配置都在这里，
    Agent 实例本身不持有状态。
    """

    # 会话标识
    session_id: str
    user_id: str

    # 参数控制（默认值从配置中心读取）
    max_iterations: int = settings.agent_max_iterations
    temperature: float = settings.llm_temperature
    max_tokens: int = settings.llm_max_tokens

    # 扩展字段
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def iteration_limit(self) -> int:
        """别名，语义更清晰"""
        return self.max_iterations


@dataclass
class AgentResult:
    """
    Agent 执行结果

    包含最终输出、工具调用记录、Token 消耗统计。
    """

    success: bool
    content: str  # 最终回答内容
    reasoning: str = ""  # 完整推理过程（累计）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # 工具调用记录
    iterations: int = 0  # 实际执行轮数
    total_tokens: int = 0  # Token 总数（累计）
    usage: dict | None = (
        None  # Token 明细（累计，含 prompt_tokens / completion_tokens / total_tokens）
    )
    error: str | None = None  # 错误信息
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseAgent(ABC):
    """
    Agent 抽象基类

    子类必须实现 _strategy_cycle() 来定义具体的推理策略。
    run() 是统一入口，负责 SSE 流式事件生成。

    用法：
        agent = ReActAgent(llm=llm_service, tools=tool_service)
        async for event in agent.run("查询天气", context=ctx):
            yield event  # 转发 SSE 事件
    """

    def __init__(self, llm: LLMGateway, tools: ToolGateway) -> None:
        self._llm = llm
        self._tools = tools
        self._context: AgentContext | None = None
        self._state = AgentState.IDLE
        self._tool_call_history: list[dict[str, Any]] = []
        self._result: AgentResult | None = None  # 子类在策略循环中设置

    # ===== 公开接口 =====

    @property
    def state(self) -> AgentState:
        return self._state

    # 结果属性，子类在 _strategy_cycle 中设置
    @property
    def result(self) -> AgentResult | None:
        """获取最终结果（run() 完成后调用）"""
        return self._result

    async def run(
        self,
        user_input: str,
        messages: list[dict[str, str]],
        context: AgentContext,
    ) -> AsyncGenerator[str]:
        """
        运行 Agent 主循环，流式产出 SSE 事件。

        Args:
            user_input: 用户输入
            messages: 组装好的消息列表（含 system prompt、历史、当前输入）
            context: Agent 上下文

        Yields:
            SSE 格式的流式事件字符串
        """
        self._context = context
        self._state = AgentState.THINKING
        self._tool_call_history = []

        yield build_info_event("Agent 开始处理")

        try:
            async for event in self._strategy_cycle(user_input, list(messages)):
                yield event

            self._state = (
                AgentState.COMPLETED
                if self._result and self._result.success
                else AgentState.FAILED
            )

        except asyncio.CancelledError:
            self._state = AgentState.CANCELLED
            yield build_info_event("Agent 已被取消")

        except Exception as e:  # noqa: BLE001
            self._state = AgentState.FAILED
            yield build_error_event(f"Agent 运行异常: {e!s}")

    # ===== 子类必须实现 =====

    @abstractmethod
    def _strategy_cycle(
        self,
        user_input: str,
        messages: list[dict[str, str]],
    ) -> AsyncGenerator[str]:
        """
        策略循环——子类在此实现具体的推理策略。

        - ReAct（当前）：推理 → 工具 → 推理 → ...
        - Plan-then-Execute（预留）：先规划再执行
        - Reflection（预留）：生成 → 反思 → 修正

        Args:
            user_input: 用户原始输入
            messages: 可修改的消息列表副本

        Yields:
            SSE 事件字符串
        """
        raise NotImplementedError

    # ===== 钩子方法（可选覆盖） =====

    async def on_thought(self, content: str) -> None:
        """每次 LLM 输出思考内容时调用"""

    async def on_tool_call(self, name: str, params: dict) -> None:
        """工具即将执行时调用"""

    async def on_tool_result(self, name: str, result) -> None:
        """工具执行完成后调用"""

    async def on_complete(self, result: AgentResult) -> None:
        """Agent 完成时调用"""
