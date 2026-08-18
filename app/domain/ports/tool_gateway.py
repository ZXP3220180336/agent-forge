"""工具网关端口（领域层拥有的抽象契约）。

依赖倒置：领域层 Agent 依赖本协议，能力层 ToolService 结构实现之。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ErrorCode(StrEnum):
    """工具执行错误码（系统级）。

    语义：executor 编排层的失败分类，供审计聚合与证据链可审计性
    （结论置信度可关联「工具调用失败类型」）；工具业务错误为 None，
    由 `error` 字符串承载 LLM 归因。
    """

    NOT_REGISTERED = "NOT_REGISTERED"  # 工具未注册
    JSON_PARSE = "JSON_PARSE"  # 参数 JSON 解析失败
    VALIDATION = "VALIDATION"  # 参数校验失败
    REJECTED = "REJECTED"  # 审批拒绝
    TIMEOUT = "TIMEOUT"  # 执行超时（executor 外层 wait_for，工具整体挂起）
    UNKNOWN = "UNKNOWN"  # 未捕获异常


@dataclass
class ToolResult:
    """工具执行结果（领域契约）。"""

    success: bool
    content: str
    error: str | None = None
    error_code: ErrorCode | None = None
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
