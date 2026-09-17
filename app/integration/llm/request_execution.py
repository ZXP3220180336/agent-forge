"""LLM 请求构建、准入与真实调用计划。

本模块只负责一次 Facade 调用内的请求件装配和每次真实 SDK 调用的准入。
响应解析、业务异常翻译、日志和最终 Reservation 结算仍由 LLMService 或整流器负责。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from .client import ClientManager
from .errors import _ExecutionAbort
from .execution_control import _raise_if_aborted, await_with_execution_control
from .request_budget import RequestBudgetGuard, RequestBudgetManager
from .reservation_limiter import (
    Reservation,
    ReservationLimiter,
    ReservationLimiterManager,
)
from .retry import RetryHandlerManager
from .token_counter import TiktokenTokenCounter

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from .retry import RetryHandler


def _build_chat_kwargs(
    model_key: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    tools: list[dict] | None,
    *,
    stream: bool,
    response_format: dict | None = None,
) -> dict[str, Any]:
    """构建传给 chat.completions.create 的请求参数。"""
    kwargs: dict[str, Any] = {
        "model": ClientManager.get_model(model_key),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if tools:
        kwargs["tools"] = tools
    if response_format:
        kwargs["response_format"] = response_format
    if stream:
        kwargs["stream_options"] = {"include_usage": True}
    return kwargs


def _build_event_fields(
    model_key: str,
    messages: list[dict],
    temperature: float,
    has_tools: bool,
    *,
    stream: bool,
) -> dict[str, Any]:
    """构建不含消息正文的 LLM 调用事件字段。"""
    return {
        "model": ClientManager.get_model(model_key),
        "messages_count": len(messages),
        "temperature": temperature,
        "has_tools": has_tools,
        "stream": stream,
    }


@dataclass(frozen=True)
class _CallContext:
    """主请求、fallback 与续接共享的请求上下文。"""

    client: AsyncOpenAI
    active: dict[str, Reservation]
    adaptive: bool
    prompt_tokens: int
    estimated: int
    max_tokens: int
    cancel_event: asyncio.Event | None = None
    deadline: float | None = None


async def _budget_guarded_call(
    budget_guard: RequestBudgetGuard,
    limiter: ReservationLimiter,
    ctx: _CallContext,
    kwargs: dict[str, Any],
) -> Any:
    """执行预算准入、Reservation 预留和受控 provider 调用。

    create 未启动前终止会全额撤回预留；create 已调度后没有成功响应也不能证明
    provider 未执行，因此以 settle(None) 保守关闭预留责任。迟回值由调用方接管。
    """
    _raise_if_aborted(ctx.cancel_event, ctx.deadline)
    budget_guard.validate(kwargs["model"], kwargs)

    if ctx.adaptive:
        reservation = await await_with_execution_control(
            lambda: limiter.reserve_adaptive(
                prompt_tokens=ctx.prompt_tokens,
                max_tokens=ctx.max_tokens,
            ),
            cancel_event=ctx.cancel_event,
            deadline=ctx.deadline,
        )
    else:
        reservation = await await_with_execution_control(
            lambda: limiter.reserve(estimated_tokens=ctx.estimated),
            cancel_event=ctx.cancel_event,
            deadline=ctx.deadline,
        )
    ctx.active["res"] = reservation

    try:
        _raise_if_aborted(ctx.cancel_event, ctx.deadline)
    except _ExecutionAbort:
        await reservation.cancel()
        ctx.active.pop("res", None)
        raise

    try:
        return await await_with_execution_control(
            lambda: ctx.client.chat.completions.create(**kwargs),
            cancel_event=ctx.cancel_event,
            deadline=ctx.deadline,
        )
    except _ExecutionAbort:
        await reservation.settle(None)
        ctx.active.pop("res", None)
        raise
    except asyncio.CancelledError:
        await reservation.settle(None)
        ctx.active.pop("res", None)
        raise
    except BaseException:
        await reservation.settle(None)
        ctx.active.pop("res", None)
        raise


class _RequestPlan(NamedTuple):
    """流式和非流式通道共享的请求执行计划。"""

    retry: RetryHandler
    active: dict[str, Reservation]
    ctx: _CallContext
    call_fn: Callable[[], Awaitable[Any]]
    fallback_fn: Callable[[], Awaitable[Any]] | None
    continue_fn: Callable[[str], Awaitable[Any]] | None
    event_fields: dict[str, Any]


def build_request_plan(
    *,
    model_key: str,
    messages: list[dict],
    tools: list[dict] | None,
    temperature: float,
    max_tokens: int,
    stream: bool,
    fallback_model_id: str,
    adaptive_reserve: bool,
    continuation_max_retries: int,
    response_format: dict | None = None,
    cancel_event: asyncio.Event | None = None,
    deadline: float | None = None,
) -> _RequestPlan:
    """构建一次 Facade 调用的主请求、fallback 和续接闭包。

    配置值由 LLMService 显式传入，避免本模块反向依赖 Facade。所有组装在调用通道
    的异常处理范围外完成，使配置与编码器错误继续 fail fast。

    Args:
        model_key: 主请求的配置键。
        messages: provider 消息列表。
        tools: 可选工具定义。
        temperature: 采样温度。
        max_tokens: 最大输出 token 数。
        stream: 是否构建流式请求。
        fallback_model_id: 同 provider 的备用模型 ID；空字符串表示禁用。
        adaptive_reserve: 是否使用自适应 TPM 预留。
        continuation_max_retries: 半流续接次数上限。
        response_format: 可选响应格式。
        cancel_event: 业务取消信号。
        deadline: monotonic 绝对期限。

    Returns:
        包含主请求、fallback、续接闭包及共享结算状态的请求计划。
    """
    kwargs = _build_chat_kwargs(
        model_key,
        messages,
        temperature,
        max_tokens,
        tools,
        stream=stream,
        response_format=response_format,
    )
    client = ClientManager.get_client(model_key)
    retry = RetryHandlerManager.get(model_key)
    prompt_count = TiktokenTokenCounter(ClientManager.get_model(model_key)).count_messages_tokens(messages)
    prompt_tokens = prompt_count if adaptive_reserve else 0
    estimated = 0 if adaptive_reserve else prompt_count + max_tokens
    active: dict[str, Reservation] = {}
    event_fields = _build_event_fields(
        model_key,
        messages,
        temperature,
        bool(tools),
        stream=stream,
    )
    ctx = _CallContext(
        client=client,
        active=active,
        adaptive=adaptive_reserve,
        prompt_tokens=prompt_tokens,
        estimated=estimated,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
        deadline=deadline,
    )

    def call_fn() -> Awaitable[Any]:
        return _budget_guarded_call(
            RequestBudgetManager.get(model_key),
            ReservationLimiterManager.get(model_key),
            ctx,
            kwargs,
        )

    fallback_fn = None
    if fallback_model_id:
        fallback_kwargs = {**kwargs, "model": fallback_model_id}

        def fallback_fn() -> Awaitable[Any]:
            return _budget_guarded_call(
                RequestBudgetManager.get("fallback"),
                ReservationLimiterManager.get("fallback"),
                ctx,
                fallback_kwargs,
            )

    continue_fn = None
    if stream and continuation_max_retries > 0:

        def continue_fn(prefix: str) -> Awaitable[Any]:
            return _budget_guarded_call(
                RequestBudgetManager.get(model_key),
                ReservationLimiterManager.get(model_key),
                ctx,
                {
                    **kwargs,
                    "messages": [
                        *messages,
                        {"role": "assistant", "content": prefix, "prefix": True},
                    ],
                },
            )

    return _RequestPlan(
        retry=retry,
        active=active,
        ctx=ctx,
        call_fn=call_fn,
        fallback_fn=fallback_fn,
        continue_fn=continue_fn,
        event_fields=event_fields,
    )
