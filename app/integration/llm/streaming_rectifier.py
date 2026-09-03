"""
StreamingRectifier — 流式整流重试策略

从 LLMService.async_generate 拆出：流式响应「首 token 前中断 → 整流重试」、
「已产出 content 中断 → 半流续接」的独立策略。职责（与 Facade 编排正交）：
    - create 阶段（retry.execute + 限流闭环 call_fn）
    - 整流重试循环（首 token 前才整流 / 整流上限 / cancel 不整流）
    - 半流续接链（已产出 content 带前缀续写，尽力而为，LLM-ADR-015）
    - chunk 解析分发（StreamParser → StreamResult 累积 + 事件产出）
    - settle/cancel 结算（reservation 闭环）
    - 熔断 feeding（迭代放弃时 record_failure）
    - 事件日志（llm_call）

整流条件（_should_rectify，全部满足）：
    1. 首 token 前（emitted_any=False）——已产出 token 不整流，避免重复输出
    2. 未超整流重试上限
    3. 异常可恢复（RETRYABLE / RATE_LIMITED，复用 classify_error）
    4. 用户未取消

半流续接条件（_should_continue，全部满足，见 LLM-ADR-015）：
    1. 已产出 content（finish_reason/refusal 为 None，模型未收尾）
    2. 非 reasoning 流、无 tool_call 半成品
    3. 异常可恢复（RETRYABLE / RATE_LIMITED）
    4. 未超续接轮次上限（continuation_max_retries）
    5. 用户未取消

用法（由 LLMService.async_generate 编排）：
    rectifier = StreamingRectifier  # 无状态静态类，不实例化
    async for event in rectifier.rectified_stream(
        create_fn=lambda: _rate_limited_call(...),
        retry=retry,
        cancel_event=cancel_event,
        stream_max_retries=stream_max_retries,  # 由调用方传入（如 settings 值）
        context=context,  # RectifierContext（含 result / active / event_fields，调用方构造）
        continue_fn=continue_fn,  # 半流续接请求构造（None 则禁用）
        continuation_max_retries=continuation_max_retries,  # 续接轮次上限
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

from app.integration.llm.streaming import StreamParser, ToolCallDelta
from app.platform.observability.logger import fill_llm_event_fields
from app.shared.events import (
    build_error_event,
    build_message_event,
    build_reasoning_event,
)

from .errors import ErrorCategory, classify_error
from .retry import RetryHandler

if TYPE_CHECKING:
    from app.domain.ports.llm_gateway import StreamResult
    from app.integration.llm.reservation_limiter import Reservation

# result.error 字段的截断上限：失败原因传给编排层（Agent 短路）后可能进
# AgentResult.error → API 响应，截断到安全长度（防止异常消息携带 URL 等
# 内部细节全量透传；日志侧另有 [:200] 截断，两者口径独立）。
_RESULT_ERROR_LIMIT = 500


def _describe_exception(exc: Exception) -> str:
    """异常的可读描述：message 为空（如看门狗 TimeoutError()）时回退类型名。

    result.error 以非空为「失败信号」（react 短路判定），空串会把失败误当空回；
    超时等框架异常的 str() 常为空串，必须兜底类型名。
    """
    return str(exc) or type(exc).__name__


def _stream_backoff(attempt: int, retry_after: float | None = None) -> float:
    """流式整流重试的退避延迟（配置由 StreamingRectifier.register_config 注入）。

    与 create 阶段的指数退避公式一致：base_delay × 2^attempt，
    上限 max_delay，可选随机抖动打散羊群效应。

    Retry-After 封顶语义（与 retry.py 的 _calculate_delay 对齐）：
        合理区间 `0 < retry_after <= max_delay` 内 → 尊重服务端建议；
        超出 max_delay（异常/恶意大值）→ 忽略并回退指数退避（本身已封顶），
        单次最长等待有界。
    """
    delay = min(
        StreamingRectifier._base_delay * (2**attempt),
        StreamingRectifier._max_delay,
    )
    if StreamingRectifier._use_jitter:
        delay = random.uniform(0, delay)
    if retry_after is not None and 0 < retry_after <= StreamingRectifier._max_delay:
        delay = max(delay, retry_after)
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


# 半流续接（LLM-ADR-015）辅助 =============================================

# 接缝重叠剥离上限：续接流首部与已产 content 尾部重叠检测的窗口（字符数）。
# 只缓冲该长度内的首部即可判定重叠，超过仍无法判明时按新内容产出（重复有界）。
_SEAM_OVERLAP_LIMIT = 64


class _StreamCancel(Exception):
    """内部信号：迭代中用户取消置位 → 优雅终止。

    与 asyncio.CancelledError（硬取消）区分——取消置位是业务信号，须走结算 +
    取消事件出口；硬取消仍由各 attempt 的 finally 兜底 settle(None)。
    """


@dataclass
class _ContinuationOutcome:
    """半流续接链的结果标记（_try_continuations 与调用方之间传递）。

    completed=True：链内已处理终态（续接成功 / 取消），调用方不再走放弃分支；
    error：续接链最后一次流中断异常（无续接时为 None → 调用方用原中断原因）。
    """

    completed: bool = False
    error: Exception | None = None


class _SeamStripper:
    """接缝重叠剥离器：续接流首部若与已产 content 尾部重叠则剥离重复后再产出。

    已产部分 token 已实时发给客户端，续接若从头重复会造成可见拼接缝。逐 token
    缓冲并与已产尾部做最长前缀重叠比对：命中重叠且未判明 → 继续缓冲；出现不匹配
    （或缓冲达 _SEAM_OVERLAP_LIMIT）→ 剥离重叠部分产出余下内容。流结束仍完全
    命中重叠 → 视为纯重放，丢弃。语义前提：续接（DeepSeek prefix）极少长重复。
    """

    def __init__(self, prev_content: str):
        self._tail = prev_content[-_SEAM_OVERLAP_LIMIT:] if prev_content else ""
        self._pending = ""
        self._done = not bool(self._tail)

    def push(self, token: str) -> str:
        """送入一个 content token，返回本次应产出的文本（空串 = 仍在缓冲/纯重叠）。"""
        if self._done:
            return token
        self._pending += token
        overlap = _seam_overlap_len(self._tail, self._pending)
        if overlap < len(self._pending):
            # 出现不匹配：前 overlap 个字符为重叠（已在 result.content），剥离后产出
            self._done = True
            out = self._pending[overlap:]
            self._pending = ""
            return out
        if len(self._pending) >= _SEAM_OVERLAP_LIMIT:
            # 缓冲达上限仍未判明：视为新内容产出（重复有界，不无限缓冲）
            self._done = True
            out = self._pending
            self._pending = ""
            return out
        return ""

    def flush(self) -> None:
        """流自然结束仍持有缓冲（全部命中重叠且未达上限）→ 视为重叠，丢弃。"""
        self._pending = ""
        self._done = True


def _seam_overlap_len(tail: str, s: str) -> int:
    """tail 的**后缀**与 s 的**前缀**的最长重叠长度（接缝去重用）。"""
    limit = min(len(tail), len(s))
    for k in range(limit, -1, -1):
        if tail.endswith(s[:k]):
            return k
    return 0


def _should_continue(
    context: RectifierContext,
    *,
    cont_attempt: int,
    continuation_max_retries: int,
    category: ErrorCategory,
    tool_emitted: bool,
    cancel_event: asyncio.Event | None,
) -> bool:
    """判断半流中断是否可续接（LLM-ADR-015，全部满足）。

    1. 已产出 content 且模型未收尾（finish_reason/refusal 为 None）
    2. 非 reasoning 流——DeepSeek thinking 回喂约束 + prefix 续写语义未验证，保守排除
    3. 无 tool_call 半成品——partial JSON 无法跨两次请求续接
    4. 异常可恢复（RETRYABLE / RATE_LIMITED，与整流复用 classify_error）
    5. 未超续接轮次上限
    6. 用户未取消
    """
    if cont_attempt >= continuation_max_retries:
        return False
    if cancel_event and cancel_event.is_set():
        return False
    if category not in (ErrorCategory.RETRYABLE, ErrorCategory.RATE_LIMITED):
        return False
    result = context.result
    if not result.content or result.finish_reason is not None or result.refusal:
        return False
    if result.has_reasoning or result.reasoning_content:
        return False
    # tool 半成品 → 不续（partial JSON 无法跨请求续接）
    return not tool_emitted


class StreamingRectifier:
    """
    流式整流重试策略（无状态静态类，不实例化）。

    管理整流重试循环、半流续接链、判断「首 token 前才可整流 / 已产出 content 才可
    续接」、处理 emitted_any 状态、熔断器 feeding。产出 SSE 事件字符串（与
    async_generate 的 yield 契约一致）。退避配置由外层 register_config() 注入
    （Container 读 settings 后调用），子模块不直接依赖 settings。
    """

    # 流式整流退避配置（默认硬编码合理值；register_config 注入 settings 值）
    _base_delay: float = 1.0
    _max_delay: float = 30.0
    _use_jitter: bool = True
    # 首包/空闲双阈值看门狗（LLM-ADR-014）：区分「模型思考慢（首包宽）」与「流已断
    # （后续 chunk 空闲窄）」——httpx read 档只能给单一值，双阈值须应用层逐 chunk 计时。
    _first_token_timeout: float = 60.0
    _chunk_idle_timeout: float = 15.0

    @classmethod
    def register_config(
        cls,
        *,
        base_delay: float,
        max_delay: float,
        use_jitter: bool,
        first_token_timeout: float,
        chunk_idle_timeout: float,
    ) -> None:
        """注入流式整流退避配置（Container 读 settings 后调用）。

        Args:
            base_delay: 退避基数（秒）
            max_delay: 退避上限（秒）
            use_jitter: 是否启用随机抖动
            first_token_timeout: 首 chunk（首字节）等待上限（宽，覆盖思考）
            chunk_idle_timeout: 后续单 chunk 空闲上限（窄，判定断流）
        """
        cls._base_delay = base_delay
        cls._max_delay = max_delay
        cls._use_jitter = use_jitter
        cls._first_token_timeout = first_token_timeout
        cls._chunk_idle_timeout = chunk_idle_timeout

    @staticmethod
    async def rectified_stream(
        create_fn: Callable[[], Awaitable[Any]],
        retry: RetryHandler,
        cancel_event: asyncio.Event | None,
        stream_max_retries: int,
        context: RectifierContext,
        fallback_fn: Callable[[], Awaitable[Any]] | None = None,
        *,
        continue_fn: Callable[[str], Awaitable[Any]] | None = None,
        continuation_max_retries: int = 0,
    ) -> AsyncGenerator[str]:
        """产出整流/续接后的 SSE 事件流。

        Args:
            create_fn: 每次整流 attempt 重新调用（内部 reserve + create），返回流式响应
            retry: RetryHandler（create 阶段重试/熔断/fallback）
            cancel_event: 取消信号，置位时优雅终止
            stream_max_retries: 流式整流重试次数上限（首 token 前中断才整流）
            context: 整流会话共享状态（result/active/event_fields，调用方构造并持有）
            fallback_fn: fallback 降级函数（create 阶段由 retry.execute 兜底）
            continue_fn: 半流续接请求构造（LLM-ADR-015）——接收已产出 content 前缀，
                追加为 assistant 消息续写并返回新流式响应；None 则禁用续接
            continuation_max_retries: 半流续接轮次上限（默认 0=禁用）
        """
        result = context.result
        active = context.active
        # ----- create 阶段由 retry.execute() 保护，失败直接 raise -----
        for attempt in range(stream_max_retries + 1):
            # LLM-006：进入整流尝试前先检查取消——取消置位后不再发起真实
            # reserve + API 请求（create_fn 内），覆盖首次尝试（cancel 已置位
            # 不发请求）与整流重试入口（上一轮中断后取消不再整流）。与迭代内
            # 检查、整流退避后检查构成三道守卫，取消信号最快生效。
            if cancel_event and cancel_event.is_set():
                context.result.error = "用户取消"
                yield build_error_event("用户取消了请求")
                return

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
                # 失败信号传给编排层（Agent 短路）：create 失败 → 本轮 LLM 调用
                # 无结果，Agent 不应把「失败」当「空输出」继续空转重试。
                exc_text = _describe_exception(e)
                context.result.error = exc_text[:_RESULT_ERROR_LIMIT]
                yield build_error_event(f"LLM 调用失败: {exc_text}")
                return

            # ----- 迭代阶段异常不受 retry 保护，自行判断整流/续接/放弃 -----
            tool_deltas: list[ToolCallDelta] = []

            try:
                # 逐 chunk 看门狗 + 累积 + 事件产出（整流与续接共用 _drain）
                async for event in StreamingRectifier._drain(
                    response,
                    context=context,
                    tool_deltas=tool_deltas,
                    cancel_event=cancel_event,
                ):
                    yield event
                # 正常结束：合并 tool_calls + 结算退差
                if tool_deltas:
                    result.tool_calls = StreamParser.merge_tool_calls(tool_deltas)
                await StreamingRectifier._settle_active(context)

            except _StreamCancel:
                # 迭代正常中用户取消：请求在途 → 结算退差 + 记失败日志，再标记信号
                await StreamingRectifier._finish_interrupted(
                    context,
                    error="用户取消",
                    attempt_start=attempt_start,
                )
                context.result.error = "用户取消"
                yield build_error_event("用户取消了请求")
                return

            except Exception as e:  # noqa: BLE001
                # 中断收尾：结算退差 + 记录失败（请求已发出，无论整流与否都 settle）
                await StreamingRectifier._finish_interrupted(
                    context,
                    error=f"流式读取中断: {e!s}"[:200],
                    attempt_start=attempt_start,
                )
                # emitted_any 由累积产物推导：usage/finish_reason/refusal 不算
                # "首 token"（与 _apply_chunk 置位口径一致），content/reasoning/
                # tool_deltas 任一非空即视为已产出。
                emitted_any = bool(
                    result.content or result.reasoning_content or tool_deltas
                )

                # 整流条件：首 token 前 + 可恢复异常 + 未超上限 + 未取消
                if _should_rectify(
                    emitted_any, attempt, stream_max_retries, e, cancel_event
                ):
                    # RATE_LIMITED（429）中断：提取服务端 Retry-After 参与退避
                    # （封顶到 max_delay，与 retry.py 的 _calculate_delay 语义一致），
                    # 其余类别走纯指数退避。
                    retry_after = (
                        RetryHandler._extract_retry_after(e)
                        if classify_error(e) == ErrorCategory.RATE_LIMITED
                        else None
                    )
                    await asyncio.sleep(_stream_backoff(attempt, retry_after))
                    if cancel_event and cancel_event.is_set():
                        context.result.error = "用户取消"
                        yield build_error_event("用户取消了请求")
                        return
                    # 清掉死流的元数据残留（usage/finish_reason/refusal 不算
                    # "首 token"，但整流后不能带入下一尝试；content/reasoning/
                    # tool_calls 因 emitted_any=False 本就为空，整流幂等安全）。
                    # refusal 不置 emitted_any（纯拒绝流可整流），不清理则成功
                    # 尝试残留死流拒答元数据 → 下游误判为拒答。
                    result.finish_reason = None
                    result.usage = None
                    result.refusal = None
                    tool_deltas.clear()
                    continue

                # LLM-011：放弃分支先判取消——_should_rectify 可能因用户取消返回
                # False（且取消后异常仍是 RETRYABLE），用户取消非下游故障，不喂
                # 熔断器（与 test_cancel_event_not_feeds_breaker 契约一致）。
                if cancel_event and cancel_event.is_set():
                    context.result.error = "用户取消"
                    yield build_error_event("用户取消了请求")
                    return

                # ----- 半流续接（LLM-ADR-015）：已产出 content 且满足边界则尽力而为
                # 续写（带前缀重发，不整轮重启）。续接链内已处理终态（completed）→
                # 直接结束；否则退化到下方放弃分支——部分 content 保留 + 失败信号，
                # 行为不劣化于现状。
                final_error: Exception = e
                if continue_fn is not None and continuation_max_retries > 0:
                    outcome = _ContinuationOutcome()
                    async for event in StreamingRectifier._try_continuations(
                        context,
                        continue_fn=continue_fn,
                        cancel_event=cancel_event,
                        continuation_max_retries=continuation_max_retries,
                        first_category=classify_error(e),
                        first_error=e,
                        first_tool_emitted=bool(tool_deltas),
                        outcome=outcome,
                    ):
                        yield event
                    if outcome.completed:
                        return
                    if outcome.error is not None:
                        final_error = outcome.error

                # 流中断放弃（不整流/不续接）→ 失败信号传编排层（Agent 短路）。
                # 已产出的部分 content 保留在 result 中，Agent 短路时一并带回。
                if classify_error(final_error) == ErrorCategory.RETRYABLE:
                    retry.circuit_breaker.record_failure()
                exc_text = _describe_exception(final_error)
                context.result.error = exc_text[:_RESULT_ERROR_LIMIT]
                yield build_error_event(f"流式响应中断: {exc_text}")
                return

            finally:
                # 迭代阶段硬取消（CancelledError）兜底闭环（LLM-003）：此时 create
                # 已成功、请求已真实发出（create 失败会在 _rate_limited_call 内
                # cancel+pop，create 阶段的 CancelledError 在 create 阶段传播、不进入
                # 本 finally）——「已发出的请求」是已提交副作用，按事务语义不可回滚：
                # settle(None) 保留配额（RPM 真实消耗不退回，防客户端配额虚增→服务端
                # 429 风暴）+ 标记终态不泄漏；且 settle(None) 内部无退款 await 循环，
                # 规避取消态 finally「多 await 清理被再次打断」的 asyncio 陷阱。
                res = active.pop("res", None)
                if res is not None and not res.settled:
                    await res.settle(None)

            # 成功：清掉整流/续接失败尝试残留的 error（同一 record 复用）
            await StreamingRectifier._log_success(context, attempt_start)
            return

    @staticmethod
    async def _drain(
        response: Any,
        *,
        context: RectifierContext,
        tool_deltas: list[ToolCallDelta],
        cancel_event: asyncio.Event | None,
        seam: _SeamStripper | None = None,
    ) -> AsyncGenerator[str]:
        """迭代单个流式响应并产出 SSE 事件（整流 attempt 与续接 attempt 共用）。

        首包/空闲双阈值看门狗：首 chunk 等「首包宽」（覆盖模型思考），其后每 chunk
        等「空闲窄」（>阈值判定断流，不等 httpx read 整档）——wait_for 取消 anext →
        asyncio.TimeoutError 冒泡给调用方分类（LLM-ADR-014）。迭代中用户取消置位抛
        _StreamCancel（优雅终止信号，与硬取消 CancelledError 区分）。正常读完自然返回，
        由调用方负责合并 tool_calls + 结算。seam 非空时 content 首部经接缝重叠剥离
        （LLM-ADR-015）。
        """
        stream_iter = response.__aiter__()
        first_chunk = True
        while True:
            idle = (
                StreamingRectifier._first_token_timeout
                if first_chunk
                else StreamingRectifier._chunk_idle_timeout
            )
            try:
                chunk = await asyncio.wait_for(anext(stream_iter), idle)
            except StopAsyncIteration:
                break
            first_chunk = False

            if cancel_event and cancel_event.is_set():
                raise _StreamCancel()

            _, events = StreamingRectifier._apply_chunk(
                chunk, context.result, tool_deltas, seam=seam
            )
            for event in events:
                yield event
        if seam is not None:
            seam.flush()

    @staticmethod
    async def _log_success(context: RectifierContext, attempt_start: float) -> None:
        """记录成功事件日志（正常读完 / 续接成功共用；清 error 供同一 record 复用）。"""
        await fill_llm_event_fields(
            context.event_fields,
            success=True,
            error=None,
            duration=time.monotonic() - attempt_start,
            usage=context.result.usage,
            finish_reason=context.result.finish_reason,
        )

    @staticmethod
    async def _try_continuations(
        context: RectifierContext,
        *,
        continue_fn: Callable[[str], Awaitable[Any]],
        cancel_event: asyncio.Event | None,
        continuation_max_retries: int,
        first_category: ErrorCategory,
        first_error: Exception,
        first_tool_emitted: bool,
        outcome: _ContinuationOutcome,
    ) -> AsyncGenerator[str]:
        """半流续接链（LLM-ADR-015）：尽力而为，链内处理终态即置 outcome.completed。

        每轮以「已产出的 result.content」快照作前缀，调 continue_fn 重新请求续写
        （新 reserve，与整流同「每次真实请求单独结算」语义）。任一续接 attempt 成功
        → 结算 + 成功日志 + completed=True；create 失败（如 provider 不支持 prefix）
        → 记录日志后退出（outcome.error 保持 None，调用方用原中断原因，对用户更贴切）；
        迭代再断 → 预算内带新前缀再续，超预算/边界不满足 → 退出由调用方走放弃分支。
        """
        result = context.result
        cont_attempt = 0
        category = first_category
        error = first_error
        tool_emitted = first_tool_emitted
        while True:
            if not _should_continue(
                context,
                cont_attempt=cont_attempt,
                continuation_max_retries=continuation_max_retries,
                category=category,
                tool_emitted=tool_emitted,
                cancel_event=cancel_event,
            ):
                return

            # RATE_LIMITED（429）：提取服务端 Retry-After 参与退避（封顶 max_delay）
            retry_after = (
                RetryHandler._extract_retry_after(error)
                if category == ErrorCategory.RATE_LIMITED
                else None
            )
            await asyncio.sleep(_stream_backoff(cont_attempt, retry_after))
            if cancel_event and cancel_event.is_set():
                context.result.error = "用户取消"
                yield build_error_event("用户取消了请求")
                outcome.completed = True
                return

            cont_start = time.monotonic()
            # 死流元数据不残留（content 保留为续写前缀；finish/usage/refusal 由上一
            # 次中断可能带出，须清，与整流清理语义一致）
            result.finish_reason = None
            result.usage = None
            result.refusal = None
            cont_tool_deltas: list[ToolCallDelta] = []

            try:
                # 尽力而为：不经 retry.execute/fallback（续接非主干路径，失败即放弃）
                response = await continue_fn(result.content)
            except Exception as ce:  # noqa: BLE001
                # create 失败（如 OpenAI 兼容端点忽略/拒绝 prefix 字段）→ 记录日志后
                # 退化放弃；outcome.error 保持 None → 调用方用原中断原因。
                await StreamingRectifier._log_failure(
                    context,
                    error=f"续接 create 失败: {str(ce)[:200]}",
                    attempt_start=cont_start,
                )
                return

            cont_attempt += 1  # 真实发起一次续接才计入预算

            try:
                seam = _SeamStripper(result.content)
                async for event in StreamingRectifier._drain(
                    response,
                    context=context,
                    tool_deltas=cont_tool_deltas,
                    cancel_event=cancel_event,
                    seam=seam,
                ):
                    yield event
                if cont_tool_deltas:
                    result.tool_calls = StreamParser.merge_tool_calls(cont_tool_deltas)
                await StreamingRectifier._settle_active(context)
                await StreamingRectifier._log_success(context, cont_start)
                outcome.completed = True
                return

            except _StreamCancel:
                await StreamingRectifier._finish_interrupted(
                    context,
                    error="用户取消",
                    attempt_start=cont_start,
                )
                context.result.error = "用户取消"
                yield build_error_event("用户取消了请求")
                outcome.completed = True
                return

            except Exception as ce2:  # noqa: BLE001
                outcome.error = ce2
                await StreamingRectifier._finish_interrupted(
                    context,
                    error=f"续接读取中断: {ce2!s}"[:200],
                    attempt_start=cont_start,
                )
                category = classify_error(ce2)
                error = ce2
                tool_emitted = bool(cont_tool_deltas)
                # 预算内带新前缀继续续接；否则 while 顶部 _should_continue 放行到调用方放弃
                continue

    @staticmethod
    def _apply_chunk(
        chunk: Any,
        result: StreamResult,
        tool_deltas: list[ToolCallDelta],
        *,
        seam: _SeamStripper | None = None,
    ) -> tuple[bool, list[str]]:
        """处理单个 chunk：累积到 result、产出事件列表；返回 (是否产出 token, 事件列表)。

        seam 非空（续接 attempt）时，content token 经接缝剥离器处理——与已产 content
        尾部重叠的首部剥离后再累积/产出，避免客户端看到重复拼接（LLM-ADR-015）。
        """
        parsed = StreamParser.parse_chunk(chunk)
        events: list[str] = []
        emitted_any = False

        if parsed.reasoning_token:
            emitted_any = True
            result.reasoning_content += parsed.reasoning_token
            events.append(build_reasoning_event(parsed.reasoning_token))

        if parsed.has_reasoning:
            result.has_reasoning = True

        if parsed.message_token:
            text = parsed.message_token
            if seam is not None:
                text = seam.push(text)
            if text:
                emitted_any = True
                result.content += text
                events.append(build_message_event(text))

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
