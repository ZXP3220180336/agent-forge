"""TokenCounter 端口（领域层拥有的抽象契约）。

依赖倒置：应用层 ContextManager 依赖本协议做 token 计数，集成层以 tiktoken 实现之。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenCounter(Protocol):
    """Token 计数抽象：领域/应用层对 token 计量需求的唯一依赖面。"""

    def count_tokens(self, text: str) -> int:
        """计算单段文本的 token 数。"""
        ...

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """计算 messages 列表的总 token 数（含每条消息格式开销与末尾回复开销）。"""
        ...
