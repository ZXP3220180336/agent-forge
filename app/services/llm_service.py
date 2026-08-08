"""
LLM 服务 — 统一 Facade

职责：
    1. 保持 async_generate() 签名向后兼容
    2. 集成 ClientManager / RetryHandler / StreamParser / 业务事件日志
    3. 新增非流式 generate() 通道
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from app.config import settings
from app.core.events import (
    build_error_event,
    build_message_event,
    build_reasoning_event,
)
from app.services.llm import (
    ClientManager,
    ReservationLimiterManager,
    RetryHandlerManager,
    StreamParser,
    StructuredOutput,
)
from app.services.llm.cost_tracker import CostTracker
from app.services.llm.retry import ErrorCategory, classify_error
from app.utils.logger import log_event_async

# =====================================================================
# 辅助数据结构
# =====================================================================


class StreamResult:
    """
    单轮流式响应的累积结果。

    LLM 层仅组装原始数据，不附加业务判断。
    """

    def __init__(self) -> None:
        self.content: str = ""
        self.reasoning_content: str = ""
        self.finish_reason: str | None = None
        self.tool_calls: list[dict] = []
        self.usage: dict | None = None
        self.refusal: str | None = None


def _stream_backoff(attempt: int) -> float:
    """流式整流重试的退避延迟（复用 LLM 退避配置）。

    与 create 阶段的指数退避公式一致：base_delay × 2^attempt，
    上限 max_delay，可选随机抖动打散羊群效应。
    """
    delay = min(settings.llm_base_delay * (2**attempt), settings.llm_max_delay)
    if settings.llm_use_jitter:
        delay = random.uniform(0, delay)
    return delay


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
    """构建传给 chat.completions.create() 的请求参数。"""
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


def _build_fallback_fn(
    kwargs: dict[str, Any],
    model_key: str,
) -> Callable[[], Awaitable[Any]] | None:
    """构建 fallback 降级函数（仅主模型配置了备用模型时启用）。

    fallback 复用主调用构建的 kwargs，仅替换 model 为备用模型——
    避免手动逐参重复（stream_options / tools 等自动继承，不会遗漏）。

    fallback 是纯兜底：只在 create 阶段由 retry.execute 调用，
    流式中断的整流重试不触发额外 fallback。
    """
    if not settings.llm_fallback_model_id:
        return None

    def fallback_fn() -> Awaitable[Any]:
        fb_kwargs = dict(kwargs)
        fb_kwargs["model"] = settings.llm_fallback_model_id
        return ClientManager.get_client(model_key).chat.completions.create(**fb_kwargs)

    return fallback_fn


def _build_event_fields(
    model_key: str,
    messages: list[dict],
    temperature: float,
    has_tools: bool,
    *,
    stream: bool,
) -> dict[str, Any]:
    """构建 LLM 调用事件字段（敏感信息脱敏，只记元数据）。

    返回可变 dict，调用点按结果逐步填充 success/error/duration/tokens。
    """
    return {
        "model": ClientManager.get_model(model_key),
        "messages_count": len(messages),
        "temperature": temperature,
        "has_tools": has_tools,
        "stream": stream,
    }


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


# =====================================================================
# 请求 Token 估算（TPM 限流用）
# =====================================================================


_encoder_cache: dict[str, Any] = {}


def _get_encoder(model: str) -> Any:
    """按模型名解析 tiktoken 编码器（进程内缓存，未知模型回退 cl100k_base）。

    与 ContextManager 的编码器解析逻辑一致；此处独立缓存是因为
    LLM 层拿到的 messages 是实时最终版（ReAct 每轮追加工具结果），
    不能复用上层的上下文预算计数。
    """
    if model in _encoder_cache:
        return _encoder_cache[model]
    try:
        import tiktoken

        encoder = tiktoken.encoding_for_model(model)
    except KeyError, ImportError:
        import tiktoken

        encoder = tiktoken.get_encoding("cl100k_base")
    _encoder_cache[model] = encoder
    return encoder


def _count_prompt_tokens(
    model_key: str,
    messages: list[dict],
    max_tokens: int = 0,
) -> int:
    """估算一次 LLM 调用的 token 消耗（prompt + 输出余量，供 TPM 限流扣减）。

    prompt 口径与 ContextManager.count_messages_tokens 一致：
        每条消息 +4（格式开销）+ content token 数 + name 额外 +1；
        末尾 +2（回复格式开销）。

    输出余量：TPM 桶若只扣 prompt，输出 token 大的调用会低估实际消耗
    （限流偏宽松）。加 max_tokens 作为输出上限的保守估算——TPM 桶按
    "请求可能消耗的最大 token" 扣减，宁可高估不错放。
    """
    encoder = _get_encoder(ClientManager.get_model(model_key))
    total = 0
    for msg in messages:
        total += 4
        total += len(encoder.encode(msg.get("content", "")))
        if msg.get("name"):
            total += 1
    return total + 2 + max_tokens


# =====================================================================
# LLM 服务
# =====================================================================


class LLMService:
    """
    LLM 服务 Facade。

    构造方式（二选一）：
        1. 传统方式：LLMService(api_key, model, base_url)
        2. 生产方式：LLMService() 自动使用 ClientManager（需先注册）
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
    ):
        # 如果传入了手动参数，注册为 "main" 配置
        if api_key:
            ClientManager.register_config(
                "main",
                api_key=api_key,
                base_url=base_url or settings.llm_base_url,
                model=model or settings.llm_model_id,
            )

    # ==================================================================
    # 公有接口
    # ==================================================================

    async def async_generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        result: StreamResult | None = None,
        model_key: str = "main",
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[str]:
        """
        单轮 LLM 流式生成（Agent 专用）。

        签名向后兼容，新增 model_key / cancel_event 可选参数。

        Args:
            model_key: 使用 ClientManager 的哪个配置（main / reasoning / fast）
            cancel_event: 取消信号，置位时优雅终止

        Yields:
            str: SSE 事件字符串
        """
        kwargs = _build_chat_kwargs(
            model_key, messages, temperature, max_tokens, tools, stream=True
        )

        if result is None:
            result = StreamResult()

        # 准备重试执行器 + fallback 函数 + 日志记录（辅助函数统一构建）
        retry = RetryHandlerManager.get(model_key)
        fallback_fn = _build_fallback_fn(kwargs, model_key)
        event_fields = _build_event_fields(
            model_key, messages, temperature, bool(tools), stream=True
        )
        # client 由 ClientManager 连接池按 key 缓存复用，与整流 attempt 无关，
        # 提到循环外，避免循环内定义闭包引用循环变量（Pylance 警告）。
        client = ClientManager.get_client(model_key)

        # 客户端限流：reserve/settle 统一闭环。
        # estimated_tokens 用 tiktoken 实时数当前 messages（含此前各轮工具结果）。
        # 限流只保护主模型链路：retry.execute 内部每次重试 call_fn 都重新 reserve；
        # fallback 备用模型不参与 reserve——客户端限流防的是主模型突发，备用模型无需考虑。
        # 闭环语义（按"请求是否已发出"分界）：
        #   - create 失败/取消 → cancel() 全额退（请求未确认发出）
        #   - create 成功后一切出口 → settle(actual)，有 usage 退差、无 usage 保守保留
        # 限流预留量：默认固定形态（prompt + max_tokens）；
        # 开启 llm_adaptive_reserve 时用自适应形态（高分位估算输出，减少占桶）。
        # 结构性解耦：provider 仍收宽裕 max_tokens（不截断输出），只有限流器预留下降。
        adaptive = settings.llm_adaptive_reserve
        if adaptive:
            prompt_tokens = _count_prompt_tokens(
                model_key, messages
            )  # max_tokens=0 → prompt+2
        else:
            estimated = _count_prompt_tokens(model_key, messages, max_tokens)
        limiter = ReservationLimiterManager.get(model_key)

        # 当前活跃 reservation，跨 create 与迭代传递。定义在整流循环外：
        # 循环内各 attempt 的迭代分支与 call_fn 需要读写同一个 dict。
        active: dict[str, Any] = {}

        async def _rate_limited_call() -> Any:
            """每次真实调用主模型前先预留配额，再发起请求。

            create 失败/被取消时全额退（cancel）再 re-raise——retry.execute
            捕获最终抛出的异常，不关心 call_fn 内部是否 catch 过，重试/fallback
            判定不受影响。
            """
            if adaptive:
                res = await limiter.reserve_adaptive(
                    prompt_tokens=prompt_tokens, max_tokens=max_tokens
                )
            else:
                res = await limiter.reserve(estimated_tokens=estimated)
            active["res"] = res
            try:
                return await client.chat.completions.create(**kwargs)
            except BaseException:  # 含 CancelledError（R1：硬取消不泄漏预留）
                await res.cancel()
                active.pop("res", None)
                raise

        # ----- 整流重试循环 -----
        # create 阶段由 retry.execute() 保护（重试/熔断/fallback），失败直接 raise；
        # 迭代阶段异常不受保护，需自行判断是否整流重试。
        # 整流条件：首 token 前（emitted_any=False）中断，且异常可恢复
        # （RETRYABLE / RATE_LIMITED），且未超重试上限，且用户未取消。
        # 已产出任何 token 后中断不整流——用户已看到部分输出，整流会产生重复内容。
        stream_max_retries = settings.llm_stream_max_retries

        for attempt in range(stream_max_retries + 1):
            attempt_start = time.monotonic()

            # ----- 阶段 1：创建流式响应（带重试 + 熔断 + fallback） -----
            # call_fn 内部先 reserve 再 create：重试每次真实请求都重新预留配额；
            # fallback 不参与 reserve（备用模型防突发无意义）。
            try:
                response = await retry.execute(
                    call_fn=_rate_limited_call,
                    fallback_fn=fallback_fn,
                )
            except Exception as e:
                event_fields["success"] = False
                event_fields["error"] = str(e)[:200]
                event_fields["duration"] = time.monotonic() - attempt_start
                await log_event_async("llm_call", **event_fields)
                yield build_error_event(f"LLM 调用失败: {e!s}")
                return

            # ----- 阶段 2：逐 chunk 解析 -----
            emitted_any = False
            tool_deltas: list[Any] = []

            # 流式迭代异常不受 retry.execute() 保护（响应对象创建后重试循环已退出），
            # 此处捕获并判断是否整流重试，避免未处理异常向上泄漏到调用方。
            # try 正常结束 = 迭代成功，在块内结算；except 处理整流/失败；
            # finally 兜底硬取消（CancelledError 不被 except Exception 捕获）。
            try:
                async for chunk in response:
                    # 取消检查
                    if cancel_event and cancel_event.is_set():
                        # 请求已发出（create 成功）→ settle（退 TPM 差），非 cancel
                        res = active.pop("res", None)
                        if res is not None:
                            await res.settle((result.usage or {}).get("total_tokens"))
                        yield build_error_event("用户取消了请求")
                        event_fields["success"] = False
                        event_fields["error"] = "用户取消"
                        event_fields["duration"] = time.monotonic() - attempt_start
                        await log_event_async("llm_call", **event_fields)
                        return

                    parsed = StreamParser.parse_chunk(chunk)

                    # reasoning
                    if parsed.reasoning_token:
                        emitted_any = True
                        result.reasoning_content += parsed.reasoning_token
                        yield build_reasoning_event(parsed.reasoning_token)

                    # message
                    if parsed.message_token:
                        emitted_any = True
                        result.content += parsed.message_token
                        yield build_message_event(parsed.message_token)

                    # finish_reason
                    if parsed.finish_reason:
                        result.finish_reason = parsed.finish_reason

                    # refusal（OpenAI 流式拒答形态）
                    if parsed.refusal:
                        result.refusal = parsed.refusal

                    # tool_calls deltas
                    if parsed.tool_call_deltas:
                        emitted_any = True
                        tool_deltas.extend(parsed.tool_call_deltas)

                    # usage（最后一个 chunk）
                    if parsed.usage:
                        result.usage = parsed.usage

                # ----- try 正常结束 = 成功：本次尝试完整读完 -----
                # 合并 tool_calls + 结算退差（请求已发出，settle 而非 cancel）
                if tool_deltas:
                    result.tool_calls = StreamParser.merge_tool_calls(tool_deltas)
                res = active.pop("res", None)
                if res is not None:
                    await res.settle((result.usage or {}).get("total_tokens"))
            except Exception as e:
                event_fields["success"] = False
                event_fields["error"] = f"流式读取中断: {e!s}"[:200]
                event_fields["duration"] = time.monotonic() - attempt_start
                await log_event_async("llm_call", **event_fields)

                # 请求已发出（create 成功）→ settle，无论整流与否
                # 死流可能已带回 usage（如 test_usage_only_interrupt_rectifies），
                # 用它退差；无 usage 则保守保留。settle 在 backoff sleep 前（R8）。
                res = active.pop("res", None)
                if res is not None:
                    await res.settle((result.usage or {}).get("total_tokens"))

                # 整流条件：首 token 前 + 可恢复异常 + 未超上限 + 未取消
                if _should_rectify(
                    emitted_any, attempt, stream_max_retries, e, cancel_event
                ):
                    await asyncio.sleep(_stream_backoff(attempt))
                    if cancel_event and cancel_event.is_set():
                        yield build_error_event("用户取消了请求")
                        return
                    # 清掉死流的元数据残留（usage/finish_reason 不算"首 token"，
                    # 但整流后不能带入下一尝试；content/reasoning/tool_calls 因
                    # emitted_any=False 本就为空，整流幂等安全）
                    result.finish_reason = None
                    result.usage = None
                    # 防御性清空：当前 tool_deltas 每 attempt 重新初始化必为空，
                    # 但显式清空避免未来重构将初始化移到循环外时携带脏数据
                    tool_deltas.clear()
                    continue

                if classify_error(e) == ErrorCategory.RETRYABLE:
                    retry.circuit_breaker.record_failure()
                yield build_error_event(f"流式响应中断: {e!s}")
                return

            finally:
                # R1：迭代阶段硬取消（CancelledError，BaseException 不被上面 except
                # Exception 捕获）时兜底闭环，避免 reservation 泄漏。
                # 成功路径已在 try 内 settle+pop，此处 active 应为空；仅异常/硬取消
                # 未闭环时兜底 cancel。
                res = active.pop("res", None)
                if res is not None and not res.settled:
                    await res.cancel()

            event_fields["success"] = True
            event_fields["error"] = (
                None  # 清掉整流失败尝试残留的 error（同一 record 复用）
            )
            event_fields["prompt_tokens"] = (result.usage or {}).get("prompt_tokens")
            event_fields["completion_tokens"] = (result.usage or {}).get(
                "completion_tokens"
            )
            event_fields["total_tokens"] = (result.usage or {}).get("total_tokens")
            event_fields["finish_reason"] = result.finish_reason
            event_fields["duration"] = time.monotonic() - attempt_start
            await log_event_async("llm_call", **event_fields)
            return

    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0,
        max_tokens: int = 1024,
        response_format: dict | None = None,
        model_key: str = "fast",
    ) -> StreamResult | None:
        """
        非流式单轮生成（适合简单任务）。

        Args:
            model_key: 使用哪个模型（默认 fast，低成本）
            response_format: 结构化输出格式，如 {"type": "json_object"}

        Returns:
            StreamResult | None（调用失败返回 None）
        """
        kwargs = _build_chat_kwargs(
            model_key,
            messages,
            temperature,
            max_tokens,
            tools,
            stream=False,
            response_format=response_format,
        )

        retry = RetryHandlerManager.get(model_key)
        event_fields = _build_event_fields(
            model_key, messages, temperature, bool(tools), stream=False
        )
        client = ClientManager.get_client(model_key)
        start_time = time.monotonic()

        # 客户端限流：reserve/settle 统一闭环（与 async_generate 一致）。
        # 每次真实请求（含 retry 内部重试）都重新 reserve，create 失败全额退。
        # 自适应预留：开启 llm_adaptive_reserve 时用高分位估算输出（结构性解耦）。
        adaptive = settings.llm_adaptive_reserve
        if adaptive:
            prompt_tokens = _count_prompt_tokens(
                model_key, messages
            )  # max_tokens=0 → prompt+2
        else:
            estimated = _count_prompt_tokens(model_key, messages, max_tokens)
        limiter = ReservationLimiterManager.get(model_key)

        # 当前活跃 reservation（跨 create 与结算传递）。generate 无整流循环，
        # 但 retry 内部重试会多次调用 call_fn，需用同一 dict 让成功路径读到。
        active: dict[str, Any] = {}

        async def _rate_limited_call() -> Any:
            """每次真实调用前先预留配额，再发起请求。create 失败全额退再 re-raise。"""
            if adaptive:
                res = await limiter.reserve_adaptive(
                    prompt_tokens=prompt_tokens, max_tokens=max_tokens
                )
            else:
                res = await limiter.reserve(estimated_tokens=estimated)
            active["res"] = res
            try:
                return await client.chat.completions.create(**kwargs)
            except BaseException:
                await res.cancel()
                active.pop("res", None)
                raise

        try:
            response = await retry.execute(
                call_fn=_rate_limited_call,
            )
        except Exception as e:
            event_fields["success"] = False
            event_fields["error"] = str(e)[:200]
            event_fields["duration"] = time.monotonic() - start_time
            await log_event_async("llm_call", **event_fields)
            return None

        # 解析非流式响应
        parsed = StreamParser.parse_non_stream(response)
        sr = StreamResult()
        sr.content = parsed.get("content", "")
        sr.finish_reason = parsed.get("finish_reason")
        sr.tool_calls = parsed.get("tool_calls", [])
        sr.usage = parsed.get("usage")
        sr.refusal = parsed.get("refusal")

        # 结算退差：实际消耗 = usage.total_tokens，退还预估多余部分
        res = active.pop("res", None)
        if res is not None:
            await res.settle((sr.usage or {}).get("total_tokens"))

        event_fields["success"] = True
        event_fields["prompt_tokens"] = (sr.usage or {}).get("prompt_tokens")
        event_fields["completion_tokens"] = (sr.usage or {}).get("completion_tokens")
        event_fields["total_tokens"] = (sr.usage or {}).get("total_tokens")
        event_fields["duration"] = time.monotonic() - start_time
        await log_event_async("llm_call", **event_fields)

        return sr

    async def generate_structured(
        self,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
    ) -> dict | None:
        """
        生成结构化输出（委托 StructuredOutput.extract 三级降级）。

        能力：JSON Schema(strict) → JSON Mode → 正则提取，逐级降级。

        Args:
            messages: 消息列表
            schema: JSON Schema
            model_key: 模型标识（默认 fast）

        Returns:
            解析后的 dict，失败返回 None
        """
        return await StructuredOutput.extract(
            llm_service=self,
            messages=messages,
            schema=schema,
            model_key=model_key,
        )

    # ==================================================================
    # 开销查询
    # ==================================================================

    @staticmethod
    def calculate_cost(
        usage: dict[str, Any] | None,
        model: str = "",
    ) -> dict[str, float]:
        """
        根据用量计算成本。

        快捷方式，代理 CostTracker。
        """
        return CostTracker.calculate(usage, model)
