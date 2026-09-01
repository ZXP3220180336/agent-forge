"""项目级统一异常体系（共享内核）。

单一事实源：全项目自定义异常在此定义，集成层各模块 re-export 使用。
异常树按「可恢复性 + 业务边界」组织（对齐 docs/shared_doc/error_handling.md 契约）：

    AppError（根，带 code 错误码）
    ├── NonRetryableError   不可恢复：向上抛，调用方决策
    ├── BusinessError       业务边界：具名短路，调用方差异化处理
    └── AgentRunError       领域编排错误（定义于 error_handling.py，携带 kind，
                            由错误处理分发决策 CONTINUE/STOP/RAISE）
"""

from __future__ import annotations

from enum import StrEnum


class AppErrorCode(StrEnum):
    """对外业务错误码（供 Phase D error_handler 映射 HTTP 状态码）。

    最小集：仅当前异常实际关联的码 + INTERNAL 兜底。
    与 ErrorCategory（LLM 传输分类）和工具层 ErrorCode（工具执行系统码）正交。
    """

    INTERNAL = "INTERNAL"  # 未知内部错误（AppError 默认）
    VALIDATION = "VALIDATION"  # 参数/配置校验失败（ParameterValidationError）
    UNAUTHORIZED = "UNAUTHORIZED"  # 未认证（UnauthorizedError，401）
    FORBIDDEN = "FORBIDDEN"  # 权限不足（ForbiddenError，403）
    NOT_FOUND = "NOT_FOUND"  # 资源不存在（NotFoundError，404）
    CIRCUIT_OPEN = "CIRCUIT_OPEN"  # 熔断开启（CircuitBreakerOpenError）
    LLM_API_ERROR = "LLM_API_ERROR"  # LLM 下游不可恢复错误（LLMAPIError，openai 4xx/认证归一）
    LLM_TRUNCATED = "LLM_TRUNCATED"  # 结构化输出截断（StructuredTruncationError）
    LLM_REFUSAL = "LLM_REFUSAL"  # 模型拒答（StructuredRefusalError）
    LLM_TOOL_CALL = "LLM_TOOL_CALL"  # 模型选择调用工具（StructuredToolCallError）
    SSRF_BLOCKED = "SSRF_BLOCKED"  # SSRF 拦截（SSRFError）


class AppError(Exception):
    """项目根异常：所有自定义异常的基类，携带业务错误码。

    用法：`raise CircuitBreakerOpenError("熔断开启")`，message 为可读描述。
    子类通过类属性 `code` 覆盖错误码，未覆盖时默认 INTERNAL。
    """

    code: AppErrorCode = AppErrorCode.INTERNAL

    def __init__(self, message: str = "") -> None:
        self.message = message
        super().__init__(message)


class NonRetryableError(AppError):
    """不可恢复错误：重试/降级无意义，向上抛让调用方决策（4xx/认证/熔断/配置错误）。"""


class BusinessError(AppError):
    """业务边界错误：具名短路，调用方差异化处理（截断/拒答/工具调用等非传输错误）。"""


# =====================================================================
# 不可恢复分支
# =====================================================================


class CircuitBreakerOpenError(NonRetryableError):
    """熔断器开启时请求被拒绝（无 fallback 兜底），调用方需等待冷却或降级备用链路。"""

    code = AppErrorCode.CIRCUIT_OPEN


class LLMAPIError(NonRetryableError):
    """LLM 下游不可恢复错误：openai APIStatusError 系列的归一类型（4xx/认证/响应校验）。

    集成层 `llm_service.generate` 边界把 openai 不可恢复传输异常包装为本异常
    （`raise LLMAPIError(...) from e` 保留原始异常供诊断）。归一目标（REASON-010）：
    领域层 `except AppError` 能统一兜住集成层透出的所有业务/系统级错误。

    status_code 保留供下游判断：如 llm/errors.py 的 `is_unsupported_response_format_error`
    依赖 status_code==400 + message 关键词判定「response_format 不被支持」降级——
    不保留该降级链即断裂。status_code 为 None 表示非 HTTP 类错误
    （APIResponseValidationError / 长度截断 / 内容过滤）。
    """

    code = AppErrorCode.LLM_API_ERROR

    def __init__(self, message: str = "", status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class ParameterValidationError(NonRetryableError, ValueError):
    """参数校验失败（调用方错误，修复参数即恢复）。

    多重继承 ValueError：既入统一异常树（可被 `except AppError` 批量捕获），
    又保留 `except ValueError` 的 Python 标准语义兼容。
    """

    code = AppErrorCode.VALIDATION


# =====================================================================
# 业务边界分支
# =====================================================================


class StructuredExtractionError(BusinessError):
    """结构化提取的 API 边界失败基类（截断/拒答/工具调用），短路不进入降级链。"""


class StructuredTruncationError(StructuredExtractionError):
    """输出被 max_tokens 截断，扩 token 重试后仍不完整；由 extract 顶层捕获返回 None。"""

    code = AppErrorCode.LLM_TRUNCATED


class StructuredRefusalError(StructuredExtractionError):
    """模型拒答（内容安全策略触发），不强行 repair，调用方转安全兜底。"""

    code = AppErrorCode.LLM_REFUSAL


class StructuredToolCallError(StructuredExtractionError):
    """模型选择调用工具而非输出 JSON（finish_reason=tool_calls），调用方按工具调用处理。"""

    code = AppErrorCode.LLM_TOOL_CALL


class SSRFError(BusinessError):
    """SSRF 拦截：检测到禁止访问的目标，拒绝请求（http_api/web_browse 捕获转 ToolResult）。"""

    code = AppErrorCode.SSRF_BLOCKED


class UnauthorizedError(BusinessError):
    """未认证（401）：请求缺少或无效的认证凭证。"""

    code = AppErrorCode.UNAUTHORIZED


class ForbiddenError(BusinessError):
    """权限不足（403）：已认证但无权访问资源。"""

    code = AppErrorCode.FORBIDDEN


class NotFoundError(BusinessError):
    """资源不存在（404）：请求的目标会话/资源不存在。"""

    code = AppErrorCode.NOT_FOUND
