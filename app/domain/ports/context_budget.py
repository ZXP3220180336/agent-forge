"""上下文预算管理端口（领域层拥有的抽象契约）。

依赖倒置：领域层 Agent 依赖本协议，应用层 ContextManager 结构实现之
（横切能力——Agent 运行中的上下文预算管理统一归 context_manager，所有模式共享）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ContextBudgetPort(Protocol):
    """Agent 运行中上下文预算管理（横切能力，由 context_manager 结构实现）。

    对齐工业界 turn-aware trimming：保留 system/user 前缀 + 最近 N 轮
    assistant/tool 配对消息，token 超限时逐轮丢最旧。被 Agent 在循环中
    （模型下次调用前）调用，作为上下文护栏。
    """

    def trim_messages(
        self,
        messages: list[dict],
        *,
        max_rounds: int | None,
        max_tokens: int | None,
    ) -> None:
        """就地裁剪 messages 到预算内（轮次 + token 双层护栏）。

        Args:
            messages: Agent 循环中可变消息列表（就地修改）
            max_rounds: 保留最大轮数（None=不限）
            max_tokens: 消息总 token 上限（None=不限）
        """
        ...