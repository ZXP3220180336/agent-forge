"""
StreamingRectifier — 流式整流重试策略

从 LLMService.async_generate 拆出：流式响应「首 token 前中断 → 整流重试」、
「已产出 content 中断 → 半流续接」的独立策略。职责（与 Facade 编排正交）：
    - create 阶段（retry.execute + 限流闭环 call_fn）
    - 整流重试循环（首 token 前才整流 / 整流上限 / cancel 不整流）
    - 非整流收尾路径（已产出中断：取消守卫 → 半流续接 → 放弃，_abandon_path）
    - 半流续接链（已产出 content 带前缀续写，尽力而为，LLM-ADR-015）
    - 委托 stream_consumption 完成单流读取、累积、接缝与关闭
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
        create_fn=lambda: _budget_guarded_call(...),  # 预算准入 → 限流闭环，见 request_execution
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
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.integration.llm.streaming import StreamParser, ToolCallDelta
from app.platform.observability.logger import fill_llm_event_fields, get_logger
from app.shared.events import build_error_event
from app.shared.exceptions import ContextWindowExceededError

from .errors import ErrorCategory, _DeadlineExceeded, _StreamCancel, classify_error
from .execution_control import wait_with_execution_control
from .retry import RetryHandler
from .stream_consumption import drain_stream

if TYPE_CHECKING:
    from app.domain.ports.llm_gateway import StreamResult
    from app.integration.llm.reservation_limiter import Reservation

logger = get_logger("llm.streaming_rectifier")

# result.error 字段的截断上限：失败原因传给编排层（Agent 短路）后可能进
# AgentResult.error → API 响应，截断到安全长度（防止异常消息携带 URL 等
# 内部细节全量透传；日志侧另有 [:200] 截断，两者口径独立）。
_RESULT_ERROR_LIMIT = 500


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


# =====================================================================
# 通用小工具（整流与续接共用）
# =====================================================================


def _describe_exception(exc: Exception) -> str:
    """异常的可读描述：message 为空（如看门狗 TimeoutError()）时回退类型名。

    result.error 以非空为「失败信号」（react 短路判定），空串会把失败误当空回；
    超时等框架异常的 str() 常为空串，必须兜底类型名。
    """
    return str(exc) or type(exc).__name__


async def _backoff_sleep(
    attempt: int,
    exc: Exception,
    *,
    cancel_event: asyncio.Event | None = None,
    deadline: float | None = None,
) -> None:
    """中断后等待退避（整流与续接共用）。

    退避公式与 create 阶段一致：base_delay × 2^attempt，上限 max_delay，可选
    随机抖动打散羊群效应。RATE_LIMITED（429）时提取服务端 Retry-After 参与退避，
    封顶语义（与 retry.py 的 _calculate_delay 对齐）：合理区间
    `0 < retry_after <= max_delay` 内尊重服务端建议，超出 max_delay（异常/恶意
    大值）忽略并回退指数退避（本身已封顶），单次最长等待有界。

    cancel_event / deadline（LLM-044）：退避等待可被中断——取消/期限命中抛类型化
    终止信号（不再发起下一次整流/续接 create）。deadline 为调用方传入的 monotonic
    绝对时刻，不重算时长。
    """
    retry_after = RetryHandler._extract_retry_after(exc) if classify_error(exc) == ErrorCategory.RATE_LIMITED else None
    delay = min(
        StreamingRectifier._base_delay * (2**attempt),
        StreamingRectifier._max_delay,
    )
    if StreamingRectifier._use_jitter:
        delay = random.uniform(0, delay)
    if retry_after is not None and 0 < retry_after <= StreamingRectifier._max_delay:
        delay = max(delay, retry_after)
    await wait_with_execution_control(delay, cancel_event=cancel_event, deadline=deadline)


def _reset_dead_meta(result: StreamResult) -> None:
    """清死流元数据残留（finish_reason/usage/refusal）：整流/续接下一尝试前调用。

    死流中断可能带出收尾元数据，成功尝试若残留会被下游误判已收尾/拒答；
    content/reasoning/tool_calls 不入此列（各自受 emitted_any / tool_deltas
    清点语义约束）。
    """
    result.finish_reason = None
    result.usage = None
    result.refusal = None


# =====================================================================
# 整流判定 + 中断信号 + 会话上下文
# =====================================================================


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
        4. 用户未取消：需判断看门狗断流等传输异常恰好与用户取消并发且用户取消未被捕获的情况
    """
    if emitted_any or attempt >= stream_max_retries:
        return False
    if cancel_event and cancel_event.is_set():
        return False
    category = classify_error(exc)
    return category in (ErrorCategory.RETRYABLE, ErrorCategory.RATE_LIMITED)


# =====================================================================
# 半流续接（LLM-ADR-015）辅助
# =====================================================================


@dataclass
class _ContinuationOutcome:
    """半流续接链的结果标记（_try_continuations 与调用方之间传递）。

    completed=True：续接成功且已完成结算，调用方不再走放弃分支；
    取消与 deadline 使用类型化异常直接离开续接链，不写入本标记；
    error：续接链最后一次流中断异常（无续接时为 None → 调用方用原中断原因）。
    """

    completed: bool = False
    error: Exception | None = None


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
    6. 用户未取消：需判断看门狗断流等传输异常恰好与用户取消并发且用户取消未被捕获的情况
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

    # ------------------------------------------------------------------
    # 主编排：整流/续接流（SSE 事件 async-gen 主入口）
    # ------------------------------------------------------------------

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
        deadline: float | None = None,
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
            deadline: 整体执行期限（monotonic 绝对，LLM-044）——整流/续接 attempt 入口、
                退避、create/reserve（经 create_fn 内 ctx）与 drain_stream 读取期按同一期限
                受控；命中即执行终止（不整流/不续接），与业务取消同为类型化出口。
        """
        result = context.result
        active = context.active

        # ----- 逐 attempt：整流重试由 for + continue 回边驱动 -----
        for attempt in range(stream_max_retries + 1):
            # LLM-006：进入整流尝试前先检查取消——取消置位后不再发起真实
            # reserve + API 请求（create_fn 内），覆盖首次尝试（cancel 已置位
            # 不发请求）与整流重试入口（上一轮中断后取消不再整流）。与迭代内
            # 检查、整流退避后检查构成三道守卫，取消信号最快生效。
            if cancel_event and cancel_event.is_set():
                raise _StreamCancel(usage=result.usage)
            # LLM-044：整流/续接 attempt 入口期限终止——deadline 命中不整流/不续接，
            # 类型化冒泡（async_generate Facade 翻译为 shared），不折 SSE error 事件。
            # 入口在本轮 create 前无已耗用量（首轮 usage 为空、整流重入前 _reset_dead_meta
            # 已清）→ 不带 usage 裸抛，与 create/reserve 段 deadline 一致。
            if deadline is not None and time.monotonic() >= deadline:
                raise _DeadlineExceeded()

            attempt_start = time.monotonic()

            # ----- create 阶段由 retry.execute() 保护（重试/熔断/fallback）-----
            try:
                response = await retry.execute(
                    call_fn=create_fn,
                    fallback_fn=fallback_fn,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
            except ContextWindowExceededError as e:
                # 预算闸已在网络调用前拒绝最终请求：非传输失败或空输出，整流/续接/
                # fallback 均无意义（消息不变则必然再次超限）。记失败日志后原样上抛
                # ——调用方（react 主循环）映射为终结性 CONTEXT_EXCEEDED；不折 error
                # 事件（避免被当作可重试 LLM_FAILED；对齐非流式 generate 先 log 再 raise）。
                await StreamingRectifier._log_failure(
                    context,
                    error=f"请求上下文超限: {str(e)[:200]}",
                    attempt_start=attempt_start,
                )
                raise
            except _StreamCancel:
                # 业务取消（create/retry 阶段任一执行控制检查点命中 cancel_event）：
                # 配额已在底层按各自语义结算闭环（请求未发 cancel 全额退、已发
                # settle(None) 保守、reserve 排队 R5 循环退款），此处无需再结算，与
                # attempt 入口 / 迭代中取消一致类型化冒泡，不生成取消 SSE。
                raise _StreamCancel(usage=result.usage)
            except _DeadlineExceeded:
                # LLM-044：create/reserve 在期限受控段（_budget_guarded_call 状态机）
                # 抛的执行终止——配额已按「请求是否已开始」结算，类型化冒泡（async_generate
                # Facade 翻译 shared），不折普通 error 事件（否则被识别为 LLM_FAILED）。
                raise
            except Exception as e:  # noqa: BLE001
                await StreamingRectifier._log_failure(context, error=str(e)[:200], attempt_start=attempt_start)
                # 失败信号传给编排层（Agent 短路）：create 失败 → 本轮 LLM 调用
                # 无结果，Agent 不应把「失败」当「空输出」继续空转重试。
                exc_text = _describe_exception(e)
                context.result.error = exc_text[:_RESULT_ERROR_LIMIT]
                yield build_error_event(f"LLM 调用失败: {exc_text}")
                return

            # ----- 迭代阶段异常不受 retry 保护，自行判断整流/续接/放弃 -----
            tool_deltas: list[ToolCallDelta] = []

            # 成功标志：drain 读到 EOF 后置位——
            # 此后 try 内只剩 _finish_success（settle + best-effort log）。
            # 结算异常或硬取消须原样上抛，不得被
            # except Exception 当作可整流/可续接的流中断（成功流已读完，重发即双倍计费）。
            stream_done = False

            try:
                # 逐 chunk 看门狗 + 累积 + 事件产出（整流与续接共用 drain_stream）
                stream_events = drain_stream(
                    response,
                    result=context.result,
                    tool_deltas=tool_deltas,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    first_token_timeout=StreamingRectifier._first_token_timeout,
                    chunk_idle_timeout=StreamingRectifier._chunk_idle_timeout,
                )
                async with aclosing(stream_events):
                    async for event in stream_events:
                        yield event
                # 正常结束：合并 tool_calls + 结算退差 + 成功日志
                if tool_deltas:
                    result.tool_calls = StreamParser.merge_tool_calls(tool_deltas)
                stream_done = True
                # settle + log硬取消/异常由下方 finally 兜底，非流中断，
                # 靠 except 顶部的 stream_done 守卫原样上抛。
                await StreamingRectifier._finish_success(context, attempt_start=attempt_start)
                return
            except _StreamCancel:
                # 正常迭代中用户取消：请求在途 → 结算退差 + 记失败日志，再标记信号
                await StreamingRectifier._finish_interrupted(
                    context,
                    error="用户取消",
                    attempt_start=attempt_start,
                )
                raise _StreamCancel(usage=result.usage)
            except _DeadlineExceeded:
                # LLM-044：流读取期整体期限耗尽——请求在途：按已有 usage 结算退差后
                # 类型化冒泡（async_generate Facade 翻译 shared）。deadline 非传输故障，
                # 不整流 / 不续接；也不折普通 error 事件（避免被识别为 LLM_FAILED）。
                await StreamingRectifier._finish_interrupted(
                    context,
                    error="执行期限耗尽",
                    attempt_start=attempt_start,
                )
                # 终止信号跨 Facade 翻译后仍携带已收到的 usage，供领域层归入成本总账。
                # `drain_stream` 可能在 usage chunk 与 deadline 同时完成时先吸收该 chunk。
                e = _DeadlineExceeded(usage=result.usage)
                raise e
            except Exception as e:
                # 读完 EOF 后（stream_done）的异常仅可能来自 _finish_success
                # （settle / log）：非流中断，原样上抛（finally 已兜底）——不整流/
                # 不喂熔断，成功流绝不重发。
                if stream_done:
                    raise

                # LLM-044：中断发生且整体期限已到——不再整流/续接/放弃（这些会发起新
                # 请求或喂熔断），按已有 usage 结算后类型化终止冒泡（Facade 翻译 shared）。
                if deadline is not None and time.monotonic() >= deadline:
                    await StreamingRectifier._finish_interrupted(
                        context,
                        error="执行期限耗尽",
                        attempt_start=attempt_start,
                    )
                    raise _DeadlineExceeded(usage=result.usage)
                # 中断收尾：结算退差 + 记录失败（请求已发出，无论整流与否都 settle）
                await StreamingRectifier._finish_interrupted(
                    context,
                    error=f"流式读取中断: {e!s}"[:200],
                    attempt_start=attempt_start,
                )
                # emitted_any 由累积产物推导：usage/finish_reason/refusal 不算
                # "首 token"，content/reasoning/tool_deltas 任一非空即视为已产出
                # （累积语义在编排层从 result 状态推导，不依赖单 chunk 返回值，LLM-035）。
                emitted_any = bool(result.content or result.reasoning_content or tool_deltas)

                # 整流：首 token 前 + 可恢复异常 + 未超整流上限 + 未外部取消 → 退避后
                # 重试原请求（continue 回边到 for 下一轮，是最短的恢复路径）。
                if _should_rectify(emitted_any, attempt, stream_max_retries, e, cancel_event):
                    # LLM-044：整流退避可被 cancel/deadline 中断（wait helper 竞争）——
                    # 中断不再发起下一次整流 create。
                    try:
                        await _backoff_sleep(attempt, e, cancel_event=cancel_event, deadline=deadline)
                    except _StreamCancel:
                        raise _StreamCancel(usage=result.usage)
                    except _DeadlineExceeded:
                        raise _DeadlineExceeded(
                            usage=result.usage
                        )  # 退避中期限到：类型化终止（finally settle(None) 兜底结算）

                    # backoff 期间取消/期限判断（wait helper 醒来兜底复查，竞态复查）
                    if cancel_event and cancel_event.is_set():
                        raise _StreamCancel(usage=result.usage)
                    if deadline is not None and time.monotonic() >= deadline:
                        # 睡满后复查期限已到：与上方退避中中断（重建携 usage）同一来源——
                        # 整流失败流已读、可能已收 usage chunk（usage 不算首 token），
                        # 携带不丢成本（下方 _reset_dead_meta 即将清空，此处为最后时机）。
                        raise _DeadlineExceeded(usage=result.usage)

                    _reset_dead_meta(result)
                    # 无需清 tool_deltas：整流前置 emitted_any=False 已保证其为空，
                    # 且列表每 attempt 于循环体顶部重新分配，continue 后即全新
                    continue

                # 不整流 → 收尾路径（LLM-011 取消守卫 → 半流续接 → 放弃），
                # 由 _abandon_path 产出其全部事件，结束即整流流结束。
                abandon_events = StreamingRectifier._abandon_path(
                    context,
                    retry=retry,
                    cancel_event=cancel_event,
                    exc=e,
                    tool_emitted=bool(tool_deltas),
                    continue_fn=continue_fn,
                    continuation_max_retries=continuation_max_retries,
                    deadline=deadline,
                )
                async with aclosing(abandon_events):
                    async for event in abandon_events:
                        yield event
                return

            finally:
                # 迭代阶段硬取消（CancelledError）兜底闭环（LLM-003）：
                # 1、此时 create 已成功（create 失败会在
                #    _budget_guarded_call 限流段内 cancel+pop，create 阶段
                #    的 CancelledError 在 create 阶段传播、不进入本 finally）
                # 2、请求已真实发出「已发出的请求」是已提交副作用，按事务语义不可回滚：
                #    settle(None) 保留配额（RPM 真实消耗不退回，防客户端配额
                #    虚增→服务端429 风暴）+ 标记终态不泄漏；且 settle(None) 内
                #    部无退款 await 循环，规避取消态 finally「多 await 清理被再次打断」的 asyncio 陷阱。
                res = active.pop("res", None)
                if res is not None and not res.settled:
                    await res.settle(None)  # 请求已发出（硬取消兜底）：保留全部预留，不 cancel（防配额虚增→429）

    # ------------------------------------------------------------------
    # 中断恢复策略（整流不适用时的收尾 / 半流续接链）
    # ------------------------------------------------------------------

    @staticmethod
    async def _abandon_path(
        context: RectifierContext,
        *,
        retry: RetryHandler,
        cancel_event: asyncio.Event | None,
        exc: Exception,
        tool_emitted: bool,
        continue_fn: Callable[[str], Awaitable[Any]] | None,
        continuation_max_retries: int,
        deadline: float | None,
    ) -> AsyncGenerator[str]:
        """整流不适用时的收尾路径：取消守卫 → 半流续接（尽力而为）→ 放弃。

        整流条件被 _should_rectify 判否（已产出 / 超上限 / 不可恢复 / 已取消）后进入：
        - 取消守卫（LLM-011）：取消非下游故障，不喂熔断器
        - 半流续接（LLM-ADR-015）：已产出 content 且边界满足则带前缀续写；链内已处理
          终态（completed）即结束，不再走放弃分支
        - 放弃：RETRYABLE 喂熔断 + 失败信号传编排层（Agent 短路）；已产出的部分
          content 保留在 result 中，Agent 短路时一并带回
        本子方法产出其应产的 SSE 事件，结束即整流流结束（无 continue 回边）。
        """
        # LLM-011：放弃分支先判取消——_should_rectify 可能因用户取消返回 False
        # （且取消后异常仍是 RETRYABLE），用户取消非下游故障，不喂熔断器
        # （与 test_cancel_event_not_feeds_breaker 契约一致）。
        if cancel_event and cancel_event.is_set():
            raise _StreamCancel(usage=context.result.usage)
        if deadline is not None and time.monotonic() >= deadline:
            raise _DeadlineExceeded(usage=context.result.usage)

        final_error: Exception = exc
        if continue_fn is not None and continuation_max_retries > 0:
            # 半流续接链：带前缀重发，不整轮重启。链内已处理终态（completed）→
            # 直接结束；否则退化到下方放弃分支——部分 content 保留 + 失败信号。
            outcome = _ContinuationOutcome()
            continuation_events = StreamingRectifier._try_continuations(
                context,
                continue_fn=continue_fn,
                cancel_event=cancel_event,
                continuation_max_retries=continuation_max_retries,
                first_category=classify_error(exc),
                first_error=exc,
                first_tool_emitted=tool_emitted,
                outcome=outcome,
                deadline=deadline,
            )
            async with aclosing(continuation_events):
                async for event in continuation_events:
                    yield event
            if outcome.completed:
                return
            if outcome.error is not None:
                final_error = outcome.error

        # 放弃（不整流/不续接）→ 失败信号传编排层（Agent 短路）；部分 content 保留。
        if classify_error(final_error) == ErrorCategory.RETRYABLE:
            retry.circuit_breaker.record_failure()
        exc_text = _describe_exception(final_error)
        context.result.error = exc_text[:_RESULT_ERROR_LIMIT]
        yield build_error_event(f"流式响应中断: {exc_text}")

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
        deadline: float | None,
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
            if deadline is not None and time.monotonic() >= deadline:
                raise _DeadlineExceeded(usage=result.usage)
            if not _should_continue(
                context,
                cont_attempt=cont_attempt,
                continuation_max_retries=continuation_max_retries,
                category=category,
                tool_emitted=tool_emitted,
                cancel_event=cancel_event,
            ):
                return

            try:
                await _backoff_sleep(
                    cont_attempt,
                    error,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
            except _StreamCancel:
                # 续接退避中用户取消：与整流退避（rectified_stream 整流分支）同语义——
                # 取消非 provider 失败，类型化冒泡给 Facade，且不再发起下一次续接 create。
                raise _StreamCancel(usage=result.usage)
            except _DeadlineExceeded:
                # 续接退避中整体期限耗尽：与整流退避（rectified_stream 整流分支）同一
                # 补全语义——镜像下方睡满复查（L850 同源 result.usage）：退避前中断流
                # 可能已收 usage chunk，携带不丢成本；未产 usage 时与裸抛等价。
                raise _DeadlineExceeded(usage=result.usage)
            if cancel_event and cancel_event.is_set():
                raise _StreamCancel(usage=result.usage)
            if deadline is not None and time.monotonic() >= deadline:
                raise _DeadlineExceeded(usage=result.usage)

            cont_start = time.monotonic()
            cont_tool_deltas: list[ToolCallDelta] = []

            try:
                # 尽力而为：不经 retry.execute/fallback（续接非主干路径，失败即放弃）
                response = await continue_fn(result.content)
            except ContextWindowExceededError as ce:
                # 续接请求命中预算闸：非 create 失败（如 prefix 不被支持）——消息前缀
                # 不变则续接必然再次超限。记日志后原样上抛，由调用方终结为
                # CONTEXT_EXCEEDED；不退化到「放弃、用原中断原因」。
                await StreamingRectifier._log_failure(
                    context,
                    error=f"续接上下文超限: {str(ce)[:200]}",
                    attempt_start=cont_start,
                )
                raise
            except _StreamCancel:
                # 续接调用中（reserve 排队 / 复查）用户取消：请求未发出（预留已退款）、
                # 无副作用——与整流 attempt 入口 / 退避后取消一致，不记失败日志
                # （取消非失败，记 _log_failure 会把用户取消计入失败观测）。
                # 不走「create 失败退化放弃」（否则取消被当失败、_abandon_path 放弃
                # 分支可能喂熔断，违背 LLM-011「取消非下游故障」）。类型化冒泡，
                # 不生成取消 SSE，也不写 result.error。
                raise _StreamCancel(usage=result.usage)
            except _DeadlineExceeded:
                # 续接 create/reserve 段尚未读取新流，无本次请求 usage 可附到异常；
                # 死流的既有 usage 仍留在 result，交领域终止收尾归账。原样上抛保留
                # traceback（与整流 create 段裸重抛语义一致）。
                raise
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
            # 只有续接 create 成功、response 所有权已取得后，才清死流的收尾元数据。
            # create 前拒绝/终止仍需由领域层保留上一请求已获 usage；content 始终作为前缀。
            _reset_dead_meta(result)

            # 续接成功标志：drain 读到 EOF 后置位——此后 try 内只剩 _finish_success
            # （settle + best-effort log），结算异常或硬取消须原样上抛，不得被
            # except Exception 当作新一轮可续接的流中断（成功续接流已完整产出并
            # 透传客户端，再续即重复内容 + 双倍计费）。与整流主流路径 rectified_stream
            # 的 stream_done 守卫同语义——两条路径共用同一完成态守卫。
            cont_stream_done = False
            try:
                stream_events = drain_stream(
                    response,
                    result=context.result,
                    tool_deltas=cont_tool_deltas,
                    cancel_event=cancel_event,
                    deadline=deadline,
                    first_token_timeout=StreamingRectifier._first_token_timeout,
                    chunk_idle_timeout=StreamingRectifier._chunk_idle_timeout,
                    seam_prefix=result.content,
                )
                async with aclosing(stream_events):
                    async for event in stream_events:
                        yield event
                if cont_tool_deltas:
                    result.tool_calls = StreamParser.merge_tool_calls(cont_tool_deltas)
                cont_stream_done = True
                await StreamingRectifier._finish_success(context, attempt_start=cont_start)
                outcome.completed = True
                return
            except _StreamCancel:
                await StreamingRectifier._finish_interrupted(
                    context,
                    error="用户取消",
                    attempt_start=cont_start,
                )
                raise _StreamCancel(usage=result.usage)
            except _DeadlineExceeded:
                await StreamingRectifier._finish_interrupted(
                    context,
                    error="执行期限耗尽",
                    attempt_start=cont_start,
                )
                raise _DeadlineExceeded(usage=result.usage)
            except Exception as ce2:
                # 读完 EOF 后（cont_stream_done）的异常仅可能来自 _finish_success
                # （settle / log）：非续接流中断，原样上抛——不置 outcome.error、不喂
                # 熔断、不再续接（成功续接流绝不重发）。reservation 结算由 rectified_stream
                # 外层 finally 兜底，与主流路径 stream_done 守卫语义一致。
                if cont_stream_done:
                    raise

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

    # ------------------------------------------------------------------
    # 结算闭环 + 事件日志
    # ------------------------------------------------------------------

    @staticmethod
    async def _finish_success(
        context: RectifierContext,
        *,
        attempt_start: float,
    ) -> None:
        """成功收尾：结算退差 + 记录成功日志（正常读完 / 续接成功共用，与
        _finish_interrupted 对称）。"""
        await StreamingRectifier._settle_active(context)
        await StreamingRectifier._log_success(context, attempt_start)

    @staticmethod
    async def _finish_interrupted(
        context: RectifierContext,
        *,
        error: str,
        attempt_start: float,
    ) -> None:
        """中断收尾：结算退差 + 记录失败日志（用户取消/流中断共用）。"""
        await StreamingRectifier._settle_active(context)
        await StreamingRectifier._log_failure(context, error=error, attempt_start=attempt_start)

    @staticmethod
    async def _settle_active(context: RectifierContext) -> None:
        """结算退差：请求已发出（create 成功）→ settle（退 TPM 差），非 cancel。

        settle 退款 await 期间被硬取消（CancelledError）时，reservation 保持
        未终态（reservation_limiter 的终态标记设计），必须塞回 active 交给
        rectified_stream 的 finally 兜底 settle(None) 保守收尾（保留剩余预留 +
        标记终态，非 cancel 全额退）——否则 res 已从 active 弹出、finally pop 到
        None，未终态 res 无归属，配额永久泄漏。
        """
        res = context.active.pop("res", None)
        if res is not None:
            try:
                await res.settle((context.result.usage or {}).get("total_tokens"))  # 按实际 usage 退 TPM 差（RPM 不退）
            except BaseException:
                # settle 中途被取消 → 未终态 res 塞回 active，由 finally 兜底 settle(None) 收尾
                context.active["res"] = res
                raise

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
