"""LLM 网关端口（领域层拥有的抽象契约）。

依赖倒置：领域层 Agent 依赖本协议，能力层 LLMService 结构实现之。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Protocol, runtime_checkable


class StreamResult:
    """LLM 单轮流式生成的结果载体（领域契约）。"""

    def __init__(self) -> None:
        self.content: str = ""
        self.reasoning_content: str = ""
        self.finish_reason: str | None = None
        self.tool_calls: list[dict] = []
        self.usage: dict | None = None
        self.refusal: str | None = None


@runtime_checkable
class LLMGateway(Protocol):
    """LLM 网关抽象：领域层对模型调用的唯一依赖面。"""

    async def async_generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        result: StreamResult | None = None,
        model_key: str = "main",
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[str]: ...

    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0,
        max_tokens: int = 1024,
        response_format: dict | None = None,
        model_key: str = "fast",
    ) -> StreamResult | None: ...

    async def generate_structured(
        self,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
        max_tokens: int | None = None,
    ) -> dict | None: ...
