"""Agent 错误处理策略（共享内核，领域层横切能力）。

对标增强项 #23（ADR agent-error-handling）：错误分类 → 注册机制 → 分发时机 →
决策动作。工业界双层策略：可恢复错误（默认回喂模型继续）vs 终结性错误
（默认终止/兜底）。默认 action = 现有行为，调用方可按 kind 注册自定义 handler。

归属：共享内核（与 exceptions.py 同层）——被领域层各 Agent 模块（BaseAgent /
ReActStrategy 等）依赖，不绑定单个子域，reasoning 与 agent 均可引用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from app.shared.exceptions import AppError


class AgentErrorKind(StrEnum):
    """Agent 运行错误分类（handler 分发键）。

    终结性错误（默认 STOP = 终止，现有行为）：
        LLM_FAILED       LLM 调用失败（StreamResult.error）→ 短路失败
        EMPTY_OUTPUT     空输出 → 重试（默认 CONTINUE）
        MAX_TURNS        迭代耗尽 → 兜底
        TIMEOUT          总时长超时 → 降级
        COST_EXCEEDED    累计成本超限 → 停机降级
        CANCELLED        外部取消 → 取消态
        UNKNOWN          未捕获异常 → FAILED 态
    可恢复错误（默认 CONTINUE = 回喂模型继续，现有行为）：
        TOOL_FAILED          工具执行失败 → 回喂模型自纠
        PARSE_FAILED         参数 JSON 解析失败 → 回喂模型自纠
        STRUCTURED_INVALID   final_answer 参数校验失败 → 回喂模型自纠
    """

    LLM_FAILED = "LLM_FAILED"
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    MAX_TURNS = "MAX_TURNS"
    TIMEOUT = "TIMEOUT"
    COST_EXCEEDED = "COST_EXCEEDED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    TOOL_FAILED = "TOOL_FAILED"
    PARSE_FAILED = "PARSE_FAILED"
    STRUCTURED_INVALID = "STRUCTURED_INVALID"


class AgentErrorAction(StrEnum):
    """错误处理决策动作。

    CONTINUE  继续循环（可恢复默认=回喂；终结性按各分支语义）
    STOP      终止循环（各 kind 默认终止逻辑，组装 outcome）
    RAISE     向上抛 AgentError（交调用方/上层决策）
    """

    CONTINUE = "CONTINUE"
    STOP = "STOP"
    RAISE = "RAISE"


@dataclass
class AgentErrorContext:
    """错误分发上下文（handler 入参）。"""

    kind: AgentErrorKind
    message: str
    iteration: int = 0


class AgentRunError(AppError):
    """Agent 编排错误（RAISE 动作抛出，携带 kind）。

    纳入统一异常树（继承 AppError，可被 `except AppError` 批量捕获）；
    kind 细分内部错误，`AppErrorCode` 用默认 INTERNAL（对外业务码层面不加）。
    BaseAgent.run 识别后 re-raise（handler 已决策上抛，不吞），交调用方处理。
    """

    def __init__(
        self,
        kind: AgentErrorKind,
        message: str,
        iteration: int = 0,
    ) -> None:
        self.kind = kind
        self.iteration = iteration
        super().__init__(f"[{kind.value}] {message}")
        # AppError.__init__ 会把 self.message 写成格式化串（str(e) 保留该串），
        # 此处还原原始 message 供调用方读取原始原因
        self.message = message


# handler 协议：异步函数，入参 AgentErrorContext，返回 AgentErrorAction
AgentErrorHandler = Callable[[AgentErrorContext], Awaitable[AgentErrorAction]]


# 默认 action：未注册 handler 时 = 现有行为（默认动作表是横切能力基线）
_DEFAULT_ACTIONS: dict[AgentErrorKind, AgentErrorAction] = {
    AgentErrorKind.LLM_FAILED: AgentErrorAction.STOP,
    AgentErrorKind.EMPTY_OUTPUT: AgentErrorAction.CONTINUE,
    AgentErrorKind.MAX_TURNS: AgentErrorAction.STOP,
    AgentErrorKind.TIMEOUT: AgentErrorAction.STOP,
    AgentErrorKind.COST_EXCEEDED: AgentErrorAction.STOP,
    AgentErrorKind.CANCELLED: AgentErrorAction.STOP,
    AgentErrorKind.UNKNOWN: AgentErrorAction.STOP,
    AgentErrorKind.TOOL_FAILED: AgentErrorAction.CONTINUE,
    AgentErrorKind.PARSE_FAILED: AgentErrorAction.CONTINUE,
    AgentErrorKind.STRUCTURED_INVALID: AgentErrorAction.CONTINUE,
}


class ErrorHandlerRegistry:
    """按错误类型注册 / 分发错误处理策略（共享内核，领域层横切）。

    用法：
        registry = ErrorHandlerRegistry()
        registry.register(AgentErrorKind.LLM_FAILED, my_handler)
        action = await registry.dispatch(AgentErrorKind.LLM_FAILED, ctx)
    """

    def __init__(
        self,
        handlers: dict[AgentErrorKind, AgentErrorHandler] | None = None,
    ) -> None:
        self._handlers: dict[AgentErrorKind, AgentErrorHandler] = dict(handlers or {})

    def register(self, kind: AgentErrorKind, handler: AgentErrorHandler) -> None:
        """注册 kind 的自定义 handler（覆盖默认 action）。"""
        self._handlers[kind] = handler

    def registered(self) -> list[AgentErrorKind]:
        """已注册自定义 handler 的 kind 列表。"""
        return list(self._handlers)

    async def dispatch(
        self,
        kind: AgentErrorKind,
        ctx: AgentErrorContext,
    ) -> AgentErrorAction:
        """分发错误：有注册 handler 则调用，否则用默认 action（= 现有行为）。"""
        handler = self._handlers.get(kind)
        if handler is not None:
            return await handler(ctx)
        return _DEFAULT_ACTIONS[kind]
