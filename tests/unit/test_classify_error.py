"""
classify_error 单元测试

验证 LLM 错误分类规则（白名单映射 + 默认不重试）：
    可重试（RETRYABLE）  → 网络层故障（openai 封装 + httpx 裸异常）、超时、5xx
    限流（RATE_LIMITED）  → 429
    不可重试（NON_RETRYABLE）→ 4xx、响应校验错误、长度截断、未知异常（默认兜底）
"""

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    ConflictError,
    ContentFilterFinishReasonError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

from app.integration.llm.retry import ErrorCategory, classify_error


def _http_exc(cls, status_code: int):
    """构造一个带指定状态码的 openai HTTP 异常。"""
    resp = httpx.Response(status_code, request=httpx.Request("POST", "http://x"))
    return cls("error", response=resp, body=None)


# =====================================================================
# RETRYABLE：网络层 / 超时 / 5xx
# =====================================================================


def test_timeout_error_retryable():
    assert classify_error(TimeoutError("timeout")) == ErrorCategory.RETRYABLE


def test_openai_api_timeout_retryable():
    exc = APITimeoutError(request=httpx.Request("POST", "http://x"))
    assert classify_error(exc) == ErrorCategory.RETRYABLE


def test_openai_api_connection_error_retryable():
    exc = APIConnectionError(message="conn failed", request=httpx.Request("POST", "http://x"))
    assert classify_error(exc) == ErrorCategory.RETRYABLE


def test_httpx_network_errors_retryable():
    req = httpx.Request("POST", "http://x")
    assert classify_error(httpx.ConnectError("refused", request=req)) == ErrorCategory.RETRYABLE
    assert classify_error(httpx.ReadError("reset", request=req)) == ErrorCategory.RETRYABLE
    assert classify_error(httpx.ConnectTimeout("connect timeout", request=req)) == ErrorCategory.RETRYABLE


def test_httpx_timeout_retryable():
    assert classify_error(httpx.TimeoutException("timeout")) == ErrorCategory.RETRYABLE


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_5xx_retryable(code):
    assert classify_error(_http_exc(InternalServerError, code)) == ErrorCategory.RETRYABLE


# =====================================================================
# RATE_LIMITED：429
# =====================================================================


def test_openai_rate_limit_error():
    exc = _http_exc(RateLimitError, 429)
    assert classify_error(exc) == ErrorCategory.RATE_LIMITED


class _Fake429(Exception):
    status_code = 429


def test_status_code_429_rate_limited():
    assert classify_error(_Fake429()) == ErrorCategory.RATE_LIMITED


# =====================================================================
# NON_RETRYABLE：4xx
# =====================================================================


@pytest.mark.parametrize(
    "cls, code",
    [
        (BadRequestError, 400),
        (PermissionDeniedError, 403),
        (NotFoundError, 404),
        (ConflictError, 409),
        (UnprocessableEntityError, 422),
    ],
)
def test_named_4xx_non_retryable(cls, code):
    assert classify_error(_http_exc(cls, code)) == ErrorCategory.NON_RETRYABLE


class _Fake404(Exception):
    status_code = 404


class _Fake405(Exception):
    status_code = 405


class _Fake413(Exception):
    status_code = 413


def test_unlisted_4xx_non_retryable():
    """未显式列出的 4xx（404/405/413）也应归类为不可重试。

    修复前：落入 RETRYABLE 兜底 → 白打下游 N 次并计入熔断窗口。
    """
    assert classify_error(_Fake404()) == ErrorCategory.NON_RETRYABLE
    assert classify_error(_Fake405()) == ErrorCategory.NON_RETRYABLE
    assert classify_error(_Fake413()) == ErrorCategory.NON_RETRYABLE


# =====================================================================
# NON_RETRYABLE：非 HTTP 永久性异常
# =====================================================================


def test_api_response_validation_error_non_retryable():
    from openai import APIResponseValidationError

    exc = APIResponseValidationError(
        response=httpx.Response(200, request=httpx.Request("POST", "http://x")),
        body=None,
        message="schema mismatch",
    )
    assert classify_error(exc) == ErrorCategory.NON_RETRYABLE


def test_length_finish_reason_non_retryable():
    from openai import LengthFinishReasonError
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion(
        id="x", choices=[], created=0, model="gpt-4", object="chat.completion"
    )
    assert classify_error(LengthFinishReasonError(completion=completion)) == ErrorCategory.NON_RETRYABLE


def test_content_filter_finish_reason_non_retryable():
    assert classify_error(ContentFilterFinishReasonError()) == ErrorCategory.NON_RETRYABLE


# =====================================================================
# NON_RETRYABLE：未知异常默认兜底
# =====================================================================


def test_unknown_exception_default_non_retryable():
    """未知异常（无 status_code、非已知类型）默认不可重试。

    修复前：默认 RETRYABLE → 盲目重试。修复后：默认 NON_RETRYABLE → 直接抛出。
    """
    assert classify_error(ValueError("bad arg")) == ErrorCategory.NON_RETRYABLE


class _FakeUnknown(Exception):
    """无 status_code、非任何已知类型的异常。"""


def test_custom_unknown_exception_default_non_retryable():
    assert classify_error(_FakeUnknown()) == ErrorCategory.NON_RETRYABLE
