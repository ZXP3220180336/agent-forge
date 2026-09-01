"""
llm/errors.py 单元测试：传输异常的统一理解与决策

覆盖：
    - normalize_transport_error：openai 不可恢复异常 → LLMAPIError（status_code 保留 / 无 status_code）
    - is_unsupported_response_format_error：400 + response_format/json_schema 关键词判定
    - decide_downstream_error：可恢复→降级 / openai 不可恢复→归一上抛 / 非 openai→原样上抛
    - DownstreamDecision 契约
"""

import httpx
import pytest
from openai import (
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ContentFilterFinishReasonError,
)

from app.integration.llm.errors import (
    DownstreamDecision,
    decide_downstream_error,
    is_unsupported_response_format_error,
    normalize_transport_error,
)
from app.shared.exceptions import CircuitBreakerOpenError, LLMAPIError


def _http_exc(cls, status_code: int, message: str = "error"):
    """构造一个带指定状态码与消息的 openai HTTP 异常。"""
    resp = httpx.Response(status_code, request=httpx.Request("POST", "http://x"))
    return cls(message, response=resp, body=None)


# =====================================================================
# normalize_transport_error
# =====================================================================


def test_normalize_api_status_error_keeps_status_code():
    """openai 401 → LLMAPIError，status_code 保留（response_format 400 降级判定依赖）。"""
    exc = _http_exc(AuthenticationError, 401)
    normalized = normalize_transport_error(exc)
    assert isinstance(normalized, LLMAPIError)
    assert normalized.status_code == 401


def test_normalize_non_http_nonretryable_no_status_code():
    """非 HTTP 永久性异常（内容过滤）→ LLMAPIError，status_code=None。"""
    exc = ContentFilterFinishReasonError()
    normalized = normalize_transport_error(exc)
    assert isinstance(normalized, LLMAPIError)
    assert normalized.status_code is None


def test_normalize_retryable_returns_none():
    """可恢复异常（超时）→ 不归一（重试耗尽走降级）。"""
    exc = APITimeoutError(request=httpx.Request("POST", "http://x"))
    assert normalize_transport_error(exc) is None


def test_normalize_non_openai_returns_none():
    """非 openai 异常（熔断 / 编程错误）→ 不归一，原样上抛。"""
    assert normalize_transport_error(CircuitBreakerOpenError("熔断")) is None
    assert normalize_transport_error(ValueError("编程错误")) is None


# =====================================================================
# is_unsupported_response_format_error
# =====================================================================


def test_unsupported_response_format_400_true():
    exc = _http_exc(BadRequestError, 400, "response_format is not supported")
    assert is_unsupported_response_format_error(exc) is True


def test_unsupported_json_schema_400_true():
    exc = _http_exc(BadRequestError, 400, "Unsupported json_schema")
    assert is_unsupported_response_format_error(exc) is True


def test_unsupported_other_400_false():
    exc = _http_exc(BadRequestError, 400, "Bad parameters")
    assert is_unsupported_response_format_error(exc) is False


def test_unsupported_non_400_false():
    exc = _http_exc(BadRequestError, 422, "response_format")
    assert is_unsupported_response_format_error(exc) is False


def test_unsupported_llmapi_error_400_true():
    """LLMAPIError（已归一）保留 status_code + message → 判定仍生效（降级链存活）。"""
    exc = LLMAPIError("response_format unsupported", status_code=400)
    assert is_unsupported_response_format_error(exc) is True


# =====================================================================
# decide_downstream_error
# =====================================================================


def test_decide_retryable_degrades():
    """可恢复（超时）重试耗尽 → 降级（to_raise=None，调用方 return None）。"""
    exc = APITimeoutError(request=httpx.Request("POST", "http://x"))
    decision = decide_downstream_error(exc)
    assert decision.to_raise is None
    assert decision.normalized is False


def test_decide_openai_nonretryable_normalizes():
    """openai 401 → to_raise=LLMAPIError（normalized=True，需 raise ... from 原异常）。"""
    exc = _http_exc(AuthenticationError, 401)
    decision = decide_downstream_error(exc)
    assert isinstance(decision.to_raise, LLMAPIError)
    assert decision.normalized is True


def test_decide_openai_400_unsupported_normalizes_not_degrade():
    """unsupported 400 在 decide 不降级——归一上抛，由 structured 特判降级下一级。"""
    exc = _http_exc(BadRequestError, 400, "response_format unsupported")
    decision = decide_downstream_error(exc)
    assert isinstance(decision.to_raise, LLMAPIError)
    assert decision.normalized is True


def test_decide_non_openai_raises_original():
    """非 openai（熔断）→ to_raise=原样异常（normalized=False，裸 raise 保留 traceback）。"""
    exc = CircuitBreakerOpenError("熔断")
    decision = decide_downstream_error(exc)
    assert decision.to_raise is exc
    assert decision.normalized is False


def test_decide_programming_error_raises_original():
    """编程错误（fail-fast）→ to_raise=原样异常。"""
    exc = ValueError("编程错误")
    decision = decide_downstream_error(exc)
    assert decision.to_raise is exc
    assert decision.normalized is False


# =====================================================================
# DownstreamDecision 契约
# =====================================================================


def test_downstream_decision_default_is_degrade():
    """默认构造 = 降级（to_raise=None）。"""
    decision = DownstreamDecision()
    assert decision.to_raise is None
    assert decision.normalized is False
