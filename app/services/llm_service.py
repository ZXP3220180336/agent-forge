"""
LLM 服务 — 统一 Facade

职责：
    1. 保持 async_generate() 签名向后兼容
    2. 集成 ClientManager / RetryHandler / StreamParser / LLMLogger
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
    LLMLogger,
    LLMRequestRecord,
    RetryConfig,
    RetryHandler,
    StreamParser,
)
from app.services.llm.cost_tracker import CostTracker
from app.services.llm.retry import ErrorCategory, classify_error

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


def _build_retry_handler() -> RetryHandler:
    """构建重试执行器（读配置中心，与 create 阶段的重试语义一致）。"""
    return RetryHandler(
        config=RetryConfig(
            max_retries=settings.llm_max_retries,
            base_delay=settings.llm_base_delay,
            max_delay=settings.llm_max_delay,
            use_jitter=settings.llm_use_jitter,
        ),
    )


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


def _build_log_record(
    model_key: str,
    messages: list[dict],
    temperature: float,
    has_tools: bool,
    *,
    stream: bool,
) -> LLMRequestRecord:
    """构建 LLM 调用日志记录（敏感信息脱敏，只记元数据）。"""
    return LLMRequestRecord(
        model=ClientManager.get_model(model_key),
        messages_count=len(messages),
        temperature=temperature,
        has_tools=has_tools,
        stream=stream,
    )


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
        retry = _build_retry_handler()
        fallback_fn = _build_fallback_fn(kwargs, model_key)
        log_record = _build_log_record(
            model_key, messages, temperature, bool(tools), stream=True
        )
        # client 由 ClientManager 连接池按 key 缓存复用，与整流 attempt 无关，
        # 提到循环外，避免循环内定义闭包引用循环变量（Pylance 警告）。
        client = ClientManager.get_client(model_key)

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
            try:
                response = await retry.execute(
                    call_fn=lambda: client.chat.completions.create(**kwargs),
                    fallback_fn=fallback_fn,
                )
            except Exception as e:
                log_record.success = False
                log_record.error = str(e)[:200]
                log_record.duration = time.monotonic() - attempt_start
                await LLMLogger.log_call(log_record)
                yield build_error_event(f"LLM 调用失败: {e!s}")
                return

            # ----- 阶段 2：逐 chunk 解析 -----
            emitted_any = False
            tool_deltas: list[Any] = []

            # 流式迭代异常不受 retry.execute() 保护（响应对象创建后重试循环已退出），
            # 此处捕获并判断是否整流重试，避免未处理异常向上泄漏到调用方。
            try:
                async for chunk in response:
                    # 取消检查
                    if cancel_event and cancel_event.is_set():
                        yield build_error_event("用户取消了请求")
                        log_record.success = False
                        log_record.error = "用户取消"
                        log_record.duration = time.monotonic() - attempt_start
                        await LLMLogger.log_call(log_record)
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

                    # tool_calls deltas
                    if parsed.tool_call_deltas:
                        emitted_any = True
                        tool_deltas.extend(parsed.tool_call_deltas)

                    # usage（最后一个 chunk）
                    if parsed.usage:
                        result.usage = parsed.usage
            except Exception as e:
                log_record.success = False
                log_record.error = f"流式读取中断: {e!s}"[:200]
                log_record.duration = time.monotonic() - attempt_start
                await LLMLogger.log_call(log_record)

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
                    continue

                yield build_error_event(f"流式响应中断: {e!s}")
                return

            # ----- 成功：本次尝试完整读完 -----
            # 合并 tool_calls
            if tool_deltas:
                result.tool_calls = StreamParser.merge_tool_calls(tool_deltas)

            log_record.success = True
            log_record.error = None  # 清掉整流失败尝试残留的 error（同一 record 复用）
            log_record.prompt_tokens = (result.usage or {}).get("prompt_tokens")
            log_record.completion_tokens = (result.usage or {}).get("completion_tokens")
            log_record.total_tokens = (result.usage or {}).get("total_tokens")
            log_record.finish_reason = result.finish_reason
            log_record.duration = time.monotonic() - attempt_start
            await LLMLogger.log_call(log_record)
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

        retry = _build_retry_handler()
        log_record = _build_log_record(
            model_key, messages, temperature, bool(tools), stream=False
        )

        start_time = time.monotonic()

        try:
            client = ClientManager.get_client(model_key)
            response = await retry.execute(
                call_fn=lambda: client.chat.completions.create(**kwargs),
            )
        except Exception as e:
            log_record.success = False
            log_record.error = str(e)[:200]
            log_record.duration = time.monotonic() - start_time
            await LLMLogger.log_call(log_record)
            return None

        # 解析非流式响应
        parsed = StreamParser.parse_non_stream(response)
        sr = StreamResult()
        sr.content = parsed.get("content", "")
        sr.finish_reason = parsed.get("finish_reason")
        sr.tool_calls = parsed.get("tool_calls", [])
        sr.usage = parsed.get("usage")

        log_record.success = True
        log_record.prompt_tokens = (sr.usage or {}).get("prompt_tokens")
        log_record.completion_tokens = (sr.usage or {}).get("completion_tokens")
        log_record.total_tokens = (sr.usage or {}).get("total_tokens")
        log_record.duration = time.monotonic() - start_time
        await LLMLogger.log_call(log_record)

        return sr

    async def generate_structured(
        self,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
    ) -> dict | None:
        """
        生成结构化输出（JSON Schema 模式）。

        Args:
            messages: 消息列表
            schema: JSON Schema
            model_key: 模型标识（默认 fast）

        Returns:
            解析后的 dict，失败返回 None
        """
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "strict": True,
                "schema": schema,
            },
        }

        result = await self.generate(
            messages=messages,
            temperature=0,
            max_tokens=4096,
            response_format=response_format,
            model_key=model_key,
        )
        if result and result.content:
            import json as json_mod

            try:
                return json_mod.loads(result.content)
            except json_mod.JSONDecodeError:
                return None
        return None

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
