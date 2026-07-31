"""
LLM 服务 — 统一 Facade

职责：
    1. 保持 async_generate() 签名向后兼容
    2. 集成 ClientManager / RetryHandler / StreamParser / LLMLogger
    3. 新增非流式 generate() 通道
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
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
        kwargs = {
            "model": ClientManager.get_model(model_key),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = tools

        if result is None:
            result = StreamResult()

        # 准备重试执行器
        retry = RetryHandler(
            config=RetryConfig(
                max_retries=settings.llm_max_retries,
                base_delay=settings.llm_base_delay,
                max_delay=settings.llm_max_delay,
                use_jitter=settings.llm_use_jitter,
            ),
        )

        # 准备 fallback 函数
        fallback_fn = None
        if settings.llm_fallback_model_id:
            fallback_fn = lambda: ClientManager.get_client(
                model_key
            ).chat.completions.create(
                model=settings.llm_fallback_model_id,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                **(dict(tools=tools) if tools else {}),
            )

        # 记录日志
        log_record = LLMRequestRecord(
            model=ClientManager.get_model(model_key),
            messages_count=len(messages),
            temperature=temperature,
            has_tools=bool(tools),
            stream=True,
        )

        start_time = time.monotonic()

        try:
            # ----- 创建流式响应（带重试 + 熔断 + fallback） -----
            client = ClientManager.get_client(model_key)
            response = await retry.execute(
                call_fn=lambda: client.chat.completions.create(**kwargs),
                fallback_fn=fallback_fn,
            )
        except Exception as e:
            log_record.success = False
            log_record.error = str(e)[:200]
            log_record.duration = time.monotonic() - start_time
            await LLMLogger.log_call(log_record)
            yield build_error_event(f"LLM 调用失败: {e!s}")
            return

        # ----- 逐 chunk 解析 -----
        tool_deltas: list[Any] = []

        async for chunk in response:
            # 取消检查
            if cancel_event and cancel_event.is_set():
                yield build_error_event("用户取消了请求")
                log_record.success = False
                log_record.error = "用户取消"
                log_record.duration = time.monotonic() - start_time
                await LLMLogger.log_call(log_record)
                return

            parsed = StreamParser.parse_chunk(chunk)

            # reasoning
            if parsed.reasoning_token:
                result.reasoning_content += parsed.reasoning_token
                yield build_reasoning_event(parsed.reasoning_token)

            # message
            if parsed.message_token:
                result.content += parsed.message_token
                yield build_message_event(parsed.message_token)

            # finish_reason
            if parsed.finish_reason:
                result.finish_reason = parsed.finish_reason

            # tool_calls deltas
            if parsed.tool_call_deltas:
                tool_deltas.extend(parsed.tool_call_deltas)

            # usage（最后一个 chunk）
            if parsed.usage:
                result.usage = parsed.usage

        # 合并 tool_calls
        if tool_deltas:
            result.tool_calls = StreamParser.merge_tool_calls(tool_deltas)

        # 非流式：usage 可能没被末尾 chunk 携带，从 finish_reason chunk 再检查
        # （一些 API 在 finish_reason chunk 带 usage，有些在独立 chunk）
        # StreamParser.parse_chunk 已处理两种情况

        log_record.success = True
        log_record.prompt_tokens = (result.usage or {}).get("prompt_tokens")
        log_record.completion_tokens = (result.usage or {}).get("completion_tokens")
        log_record.total_tokens = (result.usage or {}).get("total_tokens")
        log_record.finish_reason = result.finish_reason
        log_record.duration = time.monotonic() - start_time
        await LLMLogger.log_call(log_record)

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
        kwargs: dict[str, Any] = {
            "model": ClientManager.get_model(model_key),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format

        retry = RetryHandler(
            config=RetryConfig(
                max_retries=settings.llm_max_retries,
                base_delay=settings.llm_base_delay,
                max_delay=settings.llm_max_delay,
                use_jitter=settings.llm_use_jitter,
            ),
        )

        log_record = LLMRequestRecord(
            model=ClientManager.get_model(model_key),
            messages_count=len(messages),
            temperature=temperature,
            has_tools=bool(tools),
            stream=False,
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
