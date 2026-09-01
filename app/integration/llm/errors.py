"""
llm 传输异常的统一理解与决策（集成层错误处理单一归属）
======================================================

把 openai/httpx 传输异常翻译为统一语义（分类 / 归一 / 降级判定），
并对 generate 下游异常做统一决策（上抛 / 降级）——避免错误处理逻辑
散落在重试机制（retry.py）、Facade 边界（llm_service.py）、降级链
（structured.py）各处。

契约归属：
    - 分类契约 `ErrorCategory` / `ErrorClassifier` 定义于本模块——ErrorCategory
      是 LLM 传输层分类语言（该不该重试/退避/熔断），仅集成层 LLM 消费
      （领域/应用层不引用），故契约随实现归本模块，不独立成 shared 模块
      （shared 是被所有层引用的零依赖核心库，单一消费方的契约不属其列）
    - 归一目标 `LLMAPIError` 在 shared/exceptions.py（AppError 树）
本模块只依赖 shared + openai SDK，是集成层错误处理的实现归属。

消费方：
    - retry.py                可靠性机制：按 classify_error 分类决定重试策略
    - llm_service.py          Facade 边界：decide_downstream_error 归一上抛 / 降级
    - structured.py           降级链：unsupported 降级下一级 + decide_downstream_error
    - streaming_rectifier.py  整流重试：classify_error 判可恢复性
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import httpx
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    RateLimitError,
)

from app.shared.exceptions import LLMAPIError


# 分类契约（LLM 传输层语义，仅集成层 LLM 消费 → 随实现归本模块，不入 shared）
class ErrorCategory(Enum):
    """LLM 传输错误分类，决定处理策略（重试 / 退避 / 熔断）。

    RETRYABLE     网络层故障（openai 封装 / 裸 httpx）、超时、5xx
    RATE_LIMITED   429（退避重试，尊重 Retry-After，不计入熔断）
    NON_RETRYABLE 4xx、响应校验错误、token 截断、内容被过滤、未知异常（默认兜底）
    """

    RETRYABLE = "retryable"
    NON_RETRYABLE = "fatal"
    RATE_LIMITED = "rate_limited"


# classify_error 的函数签名契约（与实现同属本模块）
ErrorClassifier = Callable[[Exception], ErrorCategory]


# 可重试的具名异常：网络层故障（超时、连接错误）。无 status_code，必须显式匹配。
_RETRYABLE_EXC = (TimeoutError, APITimeoutError, APIConnectionError)
# 非 HTTP 的永久性异常：响应校验失败、token 截断、内容被过滤——重试无效。
_NON_RETRYABLE_EXC = (
    APIResponseValidationError,
    LengthFinishReasonError,
    ContentFilterFinishReasonError,
)


def classify_error(exc: Exception) -> ErrorCategory:
    """对异常进行分类（白名单映射，未知异常默认不可重试）。

    分类契约（`ErrorCategory` 枚举 + `ErrorClassifier` 签名）与实现同属本模块
    ——openai/httpx isinstance 判定是集成层细节，契约不独立成 shared 模块
    （ErrorCategory 仅集成层 LLM 消费，见模块 docstring）。

    分类规则：
        - RETRYABLE    网络层故障（openai 封装或裸 httpx）、超时、5xx
        - RATE_LIMITED 429
        - NON_RETRYABLE 4xx、响应校验错误、token 截断、内容被过滤、
                        以及未知异常（默认兜底——避免对重试无效的错误盲目重试）
    """
    # 1) 网络层：openai 封装（APITimeoutError / APIConnectionError）+ 裸 httpx 异常
    #    openai 某些路径会直接抛 httpx 异常（ConnectError/ReadError/Timeout 等），不会被封装。
    #    httpx.TimeoutException 与 httpx.NetworkError 无继承关系，需同时匹配。
    if isinstance(
        exc,
        _RETRYABLE_EXC + (httpx.TimeoutException, httpx.NetworkError),
    ):
        return ErrorCategory.RETRYABLE
    # 2) 限流
    if isinstance(exc, RateLimitError):
        return ErrorCategory.RATE_LIMITED
    # 3) HTTP 状态码（APIStatusError 及其子类都带 status_code）
    status_code = getattr(exc, "status_code", 0)
    if status_code:
        if 500 <= status_code < 600:
            return ErrorCategory.RETRYABLE
        if status_code == 429:
            return ErrorCategory.RATE_LIMITED
        if 400 <= status_code < 500:
            return ErrorCategory.NON_RETRYABLE
    # 4) 明确的非 HTTP 永久性异常
    if isinstance(exc, _NON_RETRYABLE_EXC):
        return ErrorCategory.NON_RETRYABLE
    # 5) 未知异常：默认不可重试（避免对无法恢复的错误盲目重试打下游）
    return ErrorCategory.NON_RETRYABLE


def normalize_transport_error(exc: Exception) -> LLMAPIError | None:
    """把 openai 不可恢复传输异常归一为 LLMAPIError（AppError 树），其余返回 None。

    归一发生在 retry 重试/熔断完成之后（retry 内部仍按原始异常分类/记账，
    本函数不改变 classify_error 语义）。

    归一范围：
        - `openai.APIStatusError`（4xx/认证）：携带 status_code
          （structured 的 response_format 400 降级判定依赖该字段）
        - `_NON_RETRYABLE_EXC`（响应校验 / 长度截断 / 内容过滤）：status_code=None

    不归一（返回 None，调用方原样 re-raise）：
        - 429 / 5xx / 超时（RETRYABLE / RATE_LIMITED，重试耗尽走 return None）
        - 非 openai 异常（熔断 CircuitBreakerOpenError / 编程错误，保持原语义）
    """
    if isinstance(exc, APIStatusError):
        return LLMAPIError(str(exc), status_code=exc.status_code)
    if isinstance(exc, _NON_RETRYABLE_EXC):
        return LLMAPIError(str(exc))
    return None


def is_unsupported_response_format_error(exc: Exception) -> bool:
    """判断是否「模型/网关不支持 response_format」的 400 错误。

    触发：`_build_json_schema_request` 无条件发 strict json_schema，部分模型/
    兼容网关不支持该 response_format 类型时返回 400（错误信息含 response_format
    或 json_schema 字样）。这类错误不是「模型能力不足需修复」，而是「该约束
    模式不支持」——应降级到下一级（JSON mode / 正则），而非当致命错误上抛。

    判据：400 状态码 + 错误信息含 response_format/json_schema 关键词。
    """
    if getattr(exc, "status_code", 0) != 400:
        return False
    message = str(getattr(exc, "message", "") or exc)
    lowered = message.lower()
    return "response_format" in lowered or "json_schema" in lowered


@dataclass(frozen=True)
class DownstreamDecision:
    """generate 下游异常的统一决策结果。

    to_raise 为 None = 调用方降级（return None，业务无结果）；
    否则调用方上抛——normalized=True 时 `raise to_raise from 原异常`
    链原始 openai 异常供诊断，normalized=False 时裸 raise 保留 traceback。
    """

    to_raise: Exception | None = None
    normalized: bool = False


def decide_downstream_error(exc: Exception) -> DownstreamDecision:
    """对 generate 下游异常统一决策：归一上抛 / 原样上抛 / 降级。

    决策矩阵：
        - RETRYABLE / RATE_LIMITED（可靠性层已重试耗尽）→ 降级（return None）
        - NON_RETRYABLE + openai 异常 → to_raise=LLMAPIError（normalized=True）
        - NON_RETRYABLE + 非 openai（熔断 CircuitBreakerOpenError / 编程错误）
          → to_raise=原样（normalized=False，裸 raise 保留 traceback）

    unsupported_response_format（400 降级下一级）判定不在本函数——仅
    structured 降级链需要（llm_service Facade 边界不降级下一级，仍归一
    上抛 LLMAPIError(400)），由 structured 调用
    is_unsupported_response_format_error 特判并保留降级诊断日志。
    """
    if classify_error(exc) != ErrorCategory.NON_RETRYABLE:
        return DownstreamDecision()
    normalized = normalize_transport_error(exc)
    if normalized is not None:
        return DownstreamDecision(to_raise=normalized, normalized=True)
    return DownstreamDecision(to_raise=exc)
