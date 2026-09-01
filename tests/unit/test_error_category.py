"""
llm/errors.py 分类契约测试

验证「传输层错误分类契约」随实现归 llm/errors.py 后的归属与形态：
    ErrorCategory 枚举（RETRYABLE / RATE_LIMITED / NON_RETRYABLE）
    ErrorClassifier 类型别名（Callable[[Exception], ErrorCategory]）
    classify_error 返回本模块枚举实例（同一实例，非副本）

契约背景：ErrorCategory 是「传输层分类」（该不该重试/退避/熔断），
仅集成层 LLM 消费（领域/应用层不引用），故契约随实现归本模块，
与 AgentErrorKind（Agent 编排分发）正交。
"""

import httpx
from openai import BadRequestError

from app.integration.llm.errors import ErrorCategory, ErrorClassifier, classify_error


def test_error_category_enum_values():
    """三态枚举值契约（与 retry 分类语义一一对应）。"""
    assert ErrorCategory.RETRYABLE.value == "retryable"
    assert ErrorCategory.RATE_LIMITED.value == "rate_limited"
    assert ErrorCategory.NON_RETRYABLE.value == "fatal"


def test_classifier_type_alias_is_callable():
    """ErrorClassifier 是 Callable[[Exception], ErrorCategory] 类型别名（纯类型、零依赖）。"""
    fn: ErrorClassifier = classify_error
    assert fn(Exception()) == ErrorCategory.NON_RETRYABLE  # 未知异常默认不可重试


def test_classify_error_returns_shared_category_instance():
    """集成层 classify_error 返回的是 shared 枚举实例（非副本）。

    `is` 判定确认 retry.py 与 shared 引用同一枚举对象——
    若 retry 本地另定义副本，此断言失败。
    """
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    exc = BadRequestError("bad request", response=resp, body=None)
    assert classify_error(exc) is ErrorCategory.NON_RETRYABLE
