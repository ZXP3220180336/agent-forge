"""工具网关端口（领域层拥有的抽象契约）。

依赖倒置：领域层 Agent 依赖本协议，能力层 ToolService 结构实现之。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolResult:
    """工具执行结果（领域契约）。"""

    success: bool
    content: str
    error: str | None = None
    metadata: dict[str, Any] | None = None
    execution_time: float | None = None
    retry_count: int = 0

    def __str__(self) -> str:
        if self.success:
            return self.content
        return f"错误: {self.error}"


@runtime_checkable
class ToolGateway(Protocol):
    """工具网关抽象：领域层对工具执行的依赖面。"""

    def get_openai_tools(self) -> list[dict[str, Any]]: ...

    async def execute(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
    ) -> ToolResult: ...
