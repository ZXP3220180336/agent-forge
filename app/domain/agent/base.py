# ============================================
# domain/agent/base.py - Agent 基类与数据定义
# ============================================
"""
Agent 基类定义
================

设计目标：
- 策略模式：BaseAgent.run() 是统一入口，_strategy_cycle() 由子类实现具体策略
- 运行隔离：每次 run() 传入独立身份；同一实例并发复用明确拒绝
- 流式友好：run() 统一转发策略产生的 SSE 事件并确定性关闭子生成器
- 桥接复用：子类通过 _require_context() 取得当前运行上下文

支持策略：
- ReAct：推理 → 工具 → 推理 → 工具 → ...（ReActAgent，见 executor.py）
- Plan-then-Execute：先规划再批量执行（PlannerAgent，见 planner.py）
- Reflection：生成 → 自查 → 修正（ReflectionAgent，见 reflection.py）
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import build_error_event, build_info_event
from app.shared.exceptions import (
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)
from app.shared.types import SessionId, UserId


class AgentState(Enum):
    """Agent 状态枚举"""

    IDLE = "idle"  # 空闲，等待输入
    THINKING = "thinking"  # LLM 推理中
    COMPLETED = "completed"  # 任务完成
    FAILED = "failed"  # 任务失败
    CANCELLED = "cancelled"  # 被取消


@dataclass
class AgentContext:
    """
    Agent 上下文信息（每次 run() 独立传入）

    所有 Agent 运行所需的外部依赖和配置都在这里，
    Agent 在运行期持有上下文与结果，因此只允许顺序复用。
    """

    # 会话标识
    session_id: SessionId
    user_id: UserId
    # 运行身份由 Application 或其他显式入口创建；同会话并发运行不得复用。
    run_id: str
    run_stop: asyncio.Event
    workflow_id: str | None = None
    parent_cancel_events: tuple[asyncio.Event, ...] = ()

    # 参数控制（默认值与配置默认一致；生产值由装配根注入，可被调用方覆盖）
    temperature: float = 0.2
    max_tokens: int = 4096
    max_iterations: int = 10
    max_execution_time: float | None = None  # 整个 ReAct 循环总时长上限（秒）；None=不设限
    # 说明：字段默认 None（策略层向后兼容）与配置默认 agent_timeout=300 不一致是刻意的——
    # 生产值由装配根注入，仅测试 / 脚本直连时不设限。
    max_context_rounds: int | None = None  # 上下文预算：保留最近 N 轮 assistant/tool 配对；None=不裁剪
    max_context_tokens: int | None = None  # 上下文预算：消息总 token 上限；None=不裁剪
    max_empty_retries: int = 2  # 连续空输出重试上限（0=首次空输出即终止）
    max_llm_fail_retries: int = (
        2  # LLM 失败重试上限：连续失败超过上限硬终止（0=首次失败即终止；防 handler CONTINUE 无限重试）
    )
    max_tool_protocol_retries: int = 2  # 工具调用协议修正上限（0=首次协议异常即终止）
    max_same_action_turns: int = 3  # 循环停滞检测：连续相同工具调用（工具+参数）上限
    batch_cleanup_grace: float = 1.0  # 批次首个控制异常后给在途兄弟工具的有界收尾窗口（秒）
    # 修复尝试上限：Reflection 修正（初稿 1 + 修正上限 max_refine_rounds-1）
    # 与 Planner replan（步骤失败重规划预算）共用
    max_refine_rounds: int = 2
    stream_mode: bool = True  # LLM 通道：True=流式 async_generate（默认，chat SSE 订阅者）；
    # False=非流式 generate()（后台子 Agent 无人逐 token 订阅，Phase C 编排按需置 False）。
    # 注：勿与 settings.agent_streaming 混淆——该字段仅是 agent_config 元数据出口，
    # 无行为接线；本字段是 ReAct 策略实际 LLM 通道开关。

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
    structured: dict | None = None  # 结构化最终答案（final_answer 工具产出，output_schema 启用时）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # 工具调用记录
    iterations: int = 0  # 实际执行轮数
    total_tokens: int = 0  # Token 总数（累计）
    usage: dict | None = None  # Token 明细（累计，含 prompt_tokens / completion_tokens / total_tokens）
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

    def __init__(
        self,
        llm: LLMGateway,
        tools: ToolGateway,
        error_handlers: ErrorHandlerRegistry | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        # 错误处理横切入口：领域层所有 Agent 模式共享（默认空 registry，用默认 action）
        self._error_handlers = error_handlers or ErrorHandlerRegistry()
        self._context: AgentContext | None = None
        self._state = AgentState.IDLE
        self._result: AgentResult | None = None  # 子类在策略循环中设置
        self._running = False

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
        if self._running:
            raise RuntimeError("同一个 Agent 实例不能并发运行")
        if not isinstance(context.run_id, str) or not context.run_id.strip():
            raise ValueError("AgentContext.run_id 必须是非空字符串")
        if not isinstance(context.run_stop, asyncio.Event):
            raise TypeError("AgentContext.run_stop 必须是 asyncio.Event")
        if not isinstance(context.parent_cancel_events, tuple) or any(
            not isinstance(event, asyncio.Event) for event in context.parent_cancel_events
        ):
            raise TypeError("AgentContext.parent_cancel_events 必须是 asyncio.Event 元组")
        if context.workflow_id is not None and (
            not isinstance(context.workflow_id, str) or not context.workflow_id.strip()
        ):
            raise ValueError("AgentContext.workflow_id 必须为 None 或非空字符串")
        self._running = True
        self._context = context
        self._state = AgentState.THINKING
        self._result = None

        try:
            yield build_info_event("Agent 开始处理")
            async with aclosing(self._strategy_cycle(user_input, list(messages))) as cycle:
                async for event in cycle:
                    yield event

            self._state = AgentState.COMPLETED if self._result and self._result.success else AgentState.FAILED

        except asyncio.CancelledError:
            # 外部取消 → 错误处理分发（默认 STOP = 现有取消态；handler 可 RAISE 上抛）
            action = await self._error_handlers.dispatch(
                AgentErrorKind.CANCELLED,
                AgentErrorContext(
                    kind=AgentErrorKind.CANCELLED,
                    message="Agent 已被取消",
                ),
            )
            if action == AgentErrorAction.RAISE:
                raise
            self._state = AgentState.CANCELLED
            yield build_info_event("Agent 已被取消")

        except AgentRunError:
            # handler 已决策 RAISE 的领域错误：不吞，上抛给调用方
            raise

        except ToolCancelledError:
            self._state = AgentState.CANCELLED
            raise
        except ToolDeadlineExceededError, ToolRunStoppedError:
            # 工具批次已先接管事实；保留类型给 Application/后续终态编排决策。
            self._state = AgentState.FAILED
            raise

        except Exception as e:
            # 未捕获异常 → 错误处理分发（默认 STOP = 现有 FAILED 态；handler 可 RAISE 上抛）
            action = await self._error_handlers.dispatch(
                AgentErrorKind.UNKNOWN,
                AgentErrorContext(
                    kind=AgentErrorKind.UNKNOWN,
                    message=f"{e!s}",
                ),
            )
            if action == AgentErrorAction.RAISE:
                raise
            self._state = AgentState.FAILED
            yield build_error_event(f"Agent 运行异常: {e!s}")
        finally:
            self._context = None
            self._running = False

    # ===== 受保护辅助方法 =====

    def _require_context(self) -> AgentContext:
        """返回当前运行上下文；只能在 run() 驱动的策略循环中调用。"""
        if self._context is None:
            raise RuntimeError("AgentContext 未设置")
        return self._context

    # ===== 子类必须实现 =====

    @abstractmethod
    def _strategy_cycle(
        self,
        user_input: str,
        messages: list[dict[str, str]],
    ) -> AsyncGenerator[str]:
        """
        策略循环——子类在此实现具体的推理策略。

        - ReAct：推理 → 工具 → 推理 → ...（ReActAgent，executor.py）
        - Plan-then-Execute：先规划再执行（PlannerAgent，planner.py）
        - Reflection：生成 → 反思 → 修正（ReflectionAgent，reflection.py）

        Args:
            user_input: 用户原始输入
            messages: 可修改的消息列表副本

        Yields:
            SSE 事件字符串
        """
        raise NotImplementedError
