"""app/shared/exceptions.py 统一异常体系单元测试

验证异常树结构、错误码关联、re-export 兼容。
"""

import pytest

from app.integration.llm.retry import CircuitBreakerOpenError  # re-export 兼容验证
from app.integration.llm.structured import (  # re-export 兼容验证
    StructuredRefusalError,
    StructuredToolCallError,
    StructuredTruncationError,
)
from app.integration.tools.security import SSRFError  # re-export 兼容验证
from app.integration.tools.validator import ParameterValidationError  # re-export 兼容验证
from app.shared.exceptions import (
    AppError,
    AppErrorCode,
    BusinessError,
    NonRetryableError,
    StructuredExtractionError,  # 中间基类定义在 shared（structured 不 re-export）
)

# 全量具名异常（从原模块 import，间接验证 re-export 保持路径）
ALL_ERRORS = [
    CircuitBreakerOpenError,
    ParameterValidationError,
    StructuredExtractionError,
    StructuredTruncationError,
    StructuredRefusalError,
    StructuredToolCallError,
    SSRFError,
]


def test_all_exceptions_are_app_error_subclasses():
    for exc in ALL_ERRORS:
        assert issubclass(exc, AppError)


def test_recoverability_classification():
    """不可恢复分支：熔断 / 参数校验"""
    assert issubclass(CircuitBreakerOpenError, NonRetryableError)
    assert issubclass(ParameterValidationError, NonRetryableError)
    # 业务边界分支：结构化失败 / SSRF
    assert issubclass(StructuredExtractionError, BusinessError)
    assert issubclass(StructuredTruncationError, BusinessError)
    assert issubclass(StructuredRefusalError, BusinessError)
    assert issubclass(StructuredToolCallError, BusinessError)
    assert issubclass(SSRFError, BusinessError)


def test_error_codes():
    assert CircuitBreakerOpenError.code == AppErrorCode.CIRCUIT_OPEN
    assert ParameterValidationError.code == AppErrorCode.VALIDATION
    assert StructuredTruncationError.code == AppErrorCode.LLM_TRUNCATED
    assert StructuredRefusalError.code == AppErrorCode.LLM_REFUSAL
    assert StructuredToolCallError.code == AppErrorCode.LLM_TOOL_CALL
    assert SSRFError.code == AppErrorCode.SSRF_BLOCKED
    # 中间基类未覆盖 code → 继承 AppError 默认 INTERNAL
    assert StructuredExtractionError.code == AppErrorCode.INTERNAL


def test_message_preserved():
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        raise CircuitBreakerOpenError("熔断开启")
    assert exc_info.value.message == "熔断开启"
    assert str(exc_info.value) == "熔断开启"


def test_app_error_default_code():
    """根异常默认 INTERNAL"""
    assert AppError().code == AppErrorCode.INTERNAL


def test_parameter_validation_error_caught_as_valueerror():
    """多重继承 ValueError：except ValueError 仍可捕获"""
    with pytest.raises(ValueError):
        raise ParameterValidationError("缺少必填参数")


def test_error_code_str_serialization():
    """StrEnum 序列化：Phase D error_handler 按 value 映射"""
    assert AppErrorCode.CIRCUIT_OPEN.value == "CIRCUIT_OPEN"
    assert str(AppErrorCode.VALIDATION) == "VALIDATION"


def test_business_error_hierarchy():
    """结构化异常族挂在 StructuredExtractionError 中间基类下"""
    assert issubclass(StructuredTruncationError, StructuredExtractionError)
    assert issubclass(StructuredRefusalError, StructuredExtractionError)
    assert issubclass(StructuredToolCallError, StructuredExtractionError)
