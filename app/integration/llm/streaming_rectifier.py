"""
StreamingRectifier — 流式整流重试策略

从 LLMService.async_generate 拆出：流式响应「首 token 前中断 → 整流重试」的
独立策略。职责（与 Facade 编排正交）：
    - create 阶段（retry.execute + 限流闭环 call_fn）
    - 整流重试循环（首 token 前才整流 / 已产出不整流 / 整流上限 / cancel 不整流）
    - chunk 解析分发（StreamParser → StreamResult 累积 + 事件产出）
    - settle/cancel 结算（reservation 闭环）
    - 熔断 feeding（迭代放弃时 record_failure）
    - 事件日志（llm_call）

整流条件（_should_rectify，全部满足）：
    1. 首 token 前（emitted_any=False）——已产出 token 不整流，避免重复输出
    2. 未超整流重试上限
    3. 异常可恢复（RETRYABLE / RATE_LIMITED，复用 classify_error）
    4. 用户未取消

用法（由 LLMService.async_generate 编排）：
    rectifier = StreamingRectifier  # 无状态静态类，不实例化
    async for event in rectifier.rectified_stream(
        create_fn=lambda: _rate_limited_call(...),
        retry=retry,
        cancel_event=cancel_event,
        stream_max_retries=stream_max_retries,  # 由调用方传入（如 settings 值）
        result=result,
        active=active,
        event_fields=event_fields,
    ):
        yield event
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.integration.llm.retry import ErrorCategory, classify_error
from app.integration.llm.streaming import StreamParser
from app.shared.events import (
    build_error_event,
    build_message_event,
    build_reasoning_event,
)
from app.utils.logger import fill_llm_event_fields

if TYPE_CHECKING:
    from app.domain.ports.llm_gateway import StreamResult
    from app.integration.llm.reservation_limiter import Reservation
    from app.integration.llm.retry import RetryHandler


def _stream_backoff(attempt: int) -> float:
    """流式整流重试的退避延迟（配置由 StreamingRectifier.register_config 注入）。

    与 create 阶段的指数退避公式一致：base_delay × 2^attempt，
    上限 max_delay，可选随机抖动打散羊群效应。
    """
    delay = min(
        StreamingRectifier._base_delay * (2**attempt),
        StreamingRectifier._max_delay,
    )
    if StreamingRectifier._use_jitter:
        delay = random.uniform(0, delay)
    return delay


def _should_rectify(
    emitted_any: bool,
    attempt: int,
    stream_max_retries: int,
    exc: Exception,
    cancel_event: asyncio.Event | None,
) -> bool:
    """判断迭代中断是否应整流重试。

    整流条件（全部满足）：
        1. 首 token 前（emitted_any=False）——已产出 token 不整流，避免重复输出
        2. 未超整流重试上限
        3. 异常可恢复（RETRYABLE / RATE_LIMITED，复用 classify_error）
        4. 用户未取消
    """
    if emitted_any or attempt >= stream_max_retries:
        return False
    if cancel_event and cancel_event.is_set():
        return False
    category = classify_error(exc)
    return category in (ErrorCategory.RETRYABLE, ErrorCategory.RATE_LIMITED)


@dataclass
class RectifierContext:
    """整流会话共享的可变状态（跨 attempt 传递）。

    由调用方（LLMService.async_generate）构造并持有，整流循环内读写：
    - result: StreamResult 累积输出
    - active: 活跃 reservation dict（成功 settle / 失败 cancel）
    - event_fields: 日志字段（整流循环内填充记录）
    """

    result: StreamResult
    active: dict[str, Reservation]
    event_fields: dict[str, Any]


class StreamingRectifier:
    """
    流式整流重试策略（无状态静态类，不实例化）。

    管理整流重试循环、判断「首 token 前才可整流」、处理 emitted_any 状态、
    熔断器 feeding。产出 SSE 事件字符串（与 async_generate 的 yield 契约一致）。
    退避配置由外层 register_config() 注入（Container 读 settings 后调用），
    子模块不直接依赖 settings。
    """

    # 流式整流退避配置（默认硬编码合理值；register_config 注入 settings 值）
    _base_delay: float = 1.0
    _max_delay: float = 30.0
    _use_jitter: bool = True

    @classmethod
    def register_config(
        cls,
        *,
        base_delay: float,
        max_delay: float,
        use_jitter: bool,
    ) -> None:
        """注入流式整流退避配置（Container 读 settings 后调用）。

        Args:
            base_delay: 退避基数（秒）
            max_delay: 退避上限（秒）
            use_jitter: 是否启用随机抖动
        """
        cls._base_delay = base_delay
        cls._max_delay = max_delay
        cls._use_jitter = use_jitter

    @staticmethod
    async def rectified_stream(
        create_fn: Callable[[], Awaitable[Any]],
        retry: RetryHandler,
        cancel_event: asyncio.Event | None,
        stream_max_retries: int,
        context: RectifierContext,
        fallback_fn: Callable[[], Awaitable[Any]] | None = None,
    ) -> AsyncGenerator[str]:
        """产出整流后的 SSE 事件流。

        Args:
            create_fn: 每次整流 attempt 重新调用（内部 reserve + create），返回流式响应
            retry: RetryHandler（create 阶段重试/熔断/fallback）
            cancel_event: 取消信号，置位时优雅终止
            stream_max_retries: 流式整流重试次数上限
            context: 整流会话共享状态（result/active/event_fields，调用方构造并持有）
            fallback_fn: fallback 降级函数（create 阶段由 retry.execute 兜底）
        """
        result = context.result
        active = context.active
        event_fields = context.event_fields
        # ----- create 阶段由 retry.execute() 保护，失败直接 raise -----
        for attempt in range(stream_max_retries + 1):
            attempt_start = time.monotonic()

            try:
                response = await retry.execute(
                    call_fn=create_fn,
                    fallback_fn=fallback_fn,
                )
            except Exception as e:  # noqa: BLE001
                await StreamingRectifier._log_failure(
                    context, error=str(e)[:200], attempt_start=attempt_start
                )
                yield build_error_event(f"LLM 调用失败: {e!s}")
                return

            # ----- 迭代阶段异常不受 retry 保护，自行判断整流 -----
            emitted_any = False
            tool_deltas: list[Any] = []

            try:
                async for chunk in response:
                    if cancel_event and cancel_event.is_set():
                        await StreamingRectifier._finish_interrupted(
                            context,
                            error="用户取消",
                            attempt_start=attempt_start,
                        )
                        yield build_error_event("用户取消了请求")
                        return

                    # 处理单个 chunk：累积 StreamResult + 产出事件，标记是否产出 token
                    # 累积语义：一旦产出过 token，后续 usage/finish-only chunk 不把标记冲回 False
                    chunk_emitted, events = StreamingRectifier._apply_chunk(
                        chunk, result, tool_deltas
                    )
                    emitted_any = emitted_any or chunk_emitted
                    for event in events:
                        yield event

                # 正常结束：合并 tool_calls + 结算退差
                if tool_deltas:
                    result.tool_calls = StreamParser.merge_tool_calls(tool_deltas)
                await StreamingRectifier._settle_active(context)

            except Exception as e:  # noqa: BLE001
                # 中断收尾：结算退差 + 记录失败（请求已发出，无论整流与否都 settle）
                await StreamingRectifier._finish_interrupted(
                    context,
                    error=f"流式读取中断: {e!s}"[:200],
                    attempt_start=attempt_start,
                )

                # 整流条件：首 token 前 + 可恢复异常 + 未超上限 + 未取消
                if _should_rectify(
                    emitted_any, attempt, stream_max_retries, e, cancel_event
                ):
                    await asyncio.sleep(_stream_backoff(attempt))
                    if cancel_event and cancel_event.is_set():
                        yield build_error_event("用户取消了请求")
                        return
                    # 清掉死流的元数据残留（usage/finish_reason 不算"首 token"，但整流后
                    # 不能带入下一尝试；content/reasoning/tool_calls 因 emitted_any=False
                    # 本就为空，整流幂等安全）
                    result.finish_reason = None
                    result.usage = None
                    tool_deltas.clear()
                    continue

                if classify_error(e) == ErrorCategory.RETRYABLE:
                    retry.circuit_breaker.record_failure()
                yield build_error_event(f"流式响应中断: {e!s}")
                return

            finally:
                # R1：迭代阶段硬取消（CancelledError）兜底闭环，避免 reservation 泄漏
                res = active.pop("res", None)
                if res is not None and not res.settled:
                    await res.cancel()

            # 成功：清掉整流失败尝试残留的 error（同一 record 复用）
            await fill_llm_event_fields(
                event_fields,
                success=True,
                error=None,
                duration=time.monotonic() - attempt_start,
                usage=result.usage,
                finish_reason=result.finish_reason,
            )
            return

    @staticmethod
    def _apply_chunk(
        chunk: Any, result: StreamResult, tool_deltas: list[Any]
    ) -> tuple[bool, list[str]]:
        """处理单个 chunk：累积到 result、产出事件列表；返回 (是否产出 token, 事件列表)。"""
        parsed = StreamParser.parse_chunk(chunk)
        events: list[str] = []
        emitted_any = False

        if parsed.reasoning_token:
            emitted_any = True
            result.reasoning_content += parsed.reasoning_token
            events.append(build_reasoning_event(parsed.reasoning_token))

        if parsed.message_token:
            emitted_any = True
            result.content += parsed.message_token
            events.append(build_message_event(parsed.message_token))

        if parsed.finish_reason:
            result.finish_reason = parsed.finish_reason

        if parsed.refusal:
            result.refusal = parsed.refusal

        if parsed.tool_call_deltas:
            emitted_any = True
            tool_deltas.extend(parsed.tool_call_deltas)

        if parsed.usage:
            result.usage = parsed.usage

        return emitted_any, events

    @staticmethod
    async def _settle_active(context: RectifierContext) -> None:
        """结算退差：请求已发出（create 成功）→ settle（退 TPM 差），非 cancel。

        settle 退款 await 期间被硬取消（CancelledError）时，reservation 保持
        未终态（reservation_limiter 的终态标记设计），必须塞回 active 交给
        rectified_stream 的 finally 兜底 cancel 续退——否则 res 已从 active 弹出、
        finally pop 到 None，配额永久泄漏。
        """
        res = context.active.pop("res", None)
        if res is not None:
            try:
                await res.settle((context.result.usage or {}).get("total_tokens"))
            except BaseException:
                # settle 中途被取消 → 未终态 res 塞回 active，由 finally 兜底续退
                context.active["res"] = res
                raise

    @staticmethod
    async def _finish_interrupted(
        context: RectifierContext,
        *,
        error: str,
        attempt_start: float,
    ) -> None:
        """中断收尾：结算退差 + 记录失败日志（用户取消/流中断共用）。"""
        await StreamingRectifier._settle_active(context)
        await StreamingRectifier._log_failure(
            context, error=error, attempt_start=attempt_start
        )

    @staticmethod
    async def _log_failure(
        context: RectifierContext,
        *,
        error: str,
        attempt_start: float,
    ) -> None:
        """记录失败事件日志。"""
        await fill_llm_event_fields(
            context.event_fields,
            success=False,
            error=error,
            duration=time.monotonic() - attempt_start,
        )
