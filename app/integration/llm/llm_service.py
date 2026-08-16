"""
LLM 服务 — 统一 Facade

职责：
    1. 保持 async_generate() 签名向后兼容
    2. 集成 ClientManager / RetryHandler / StreamParser / 业务事件日志
    3. 新增非流式 generate() 通道
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, ClassVar

from app.domain.ports.llm_gateway import StreamResult
from app.integration.llm import (
    ClientManager,
    ReservationLimiterManager,
    RetryHandlerManager,
    StreamingRectifier,
    StreamParser,
    StructuredOutput,
)
from app.integration.llm.cost_tracker import CostTracker
from app.integration.llm.reservation_limiter import Reservation
from app.integration.llm.retry import ErrorCategory, classify_error
from app.integration.llm.streaming_rectifier import RectifierContext
from app.utils.logger import fill_llm_event_fields

# =====================================================================
# 辅助数据结构
# =====================================================================


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

    **同 provider 约束（LLM-012）**：fallback 用 `ClientManager.get_client(model_key)`
    （主模型 client）发请求，复用主模型 base_url/密钥——仅支持「同服务商便宜模型」
    降级（如 deepseek-chat → deepseek-reasoner）。跨服务商 fallback 需独立
    base_url/api_key 配置（当前不提供），配置跨 provider 模型会打到主端点带
    备用模型名 → 400/404，fallback 静默失效。约束详见 issues/integration/llm/llm-012。
    """
    if not LLMService._fallback_model_id:
        return None

    def fallback_fn() -> Awaitable[Any]:
        fb_kwargs = dict(kwargs)
        fb_kwargs["model"] = LLMService._fallback_model_id
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


async def _rate_limited_call(
    adaptive: bool,
    limiter: Any,
    client: Any,
    kwargs: dict[str, Any],
    active: dict[str, Reservation],
    prompt_tokens: int = 0,
    estimated: int = 0,
    max_tokens: int = 0,
) -> Any:
    """限流闭环：每次真实调用主模型前先预留配额，再发起请求。

    create 失败/被取消时全额退（cancel）再 re-raise——retry.execute 捕获最终
    抛出的异常，不关心 call_fn 内部是否 catch 过，重试/fallback 判定不受影响。
    每次真实请求（含 retry 内部重试、整流重试）都重新 reserve。
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
    except KeyError:
        # 未知模型无专属编码器 → 回退通用 cl100k_base（token 估算足够）。
        # 只捕获 KeyError：tiktoken 缺失（ImportError）是硬依赖损坏，应自然
        # 传播 fail fast，不被此兜底掩盖。
        encoder = tiktoken.get_encoding("cl100k_base")
    _encoder_cache[model] = encoder
    return encoder


def _content_to_text(content: Any) -> str:
    """将消息 content 归一化为可编码文本（供 token 估算）。

    - None（工具报错等缺 content 场景）→ 空串
    - str → 原样
    - 多模态 list（OpenAI 格式 `[{"type": "text", "text": ...}, ...]`）→
      只取文本片段拼接；图片等非文本条目不参与 token 估算

    修复前 `encoder.encode(msg.get("content", ""))`：content 键存在但为
    None（`or ""` 兜不住，list 是 truthy 也不触发）时 encode 抛 TypeError，
    限流预留阶段崩溃整次调用。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts)
    # 非 str/list 的异常形状：不崩，保守回退空串（宁可低估不崩）
    return ""


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
        total += len(encoder.encode(_content_to_text(msg.get("content"))))
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

    _fallback_model_id: ClassVar[str] = ""
    _adaptive_reserve: ClassVar[bool] = False
    _stream_max_retries: ClassVar[int] = 1

    @classmethod
    def register_config(
        cls,
        *,
        fallback_model_id: str,
        adaptive_reserve: bool,
        stream_max_retries: int,
    ) -> None:
        """注入运行期配置（由装配根调用，避免直接依赖 settings）。"""
        cls._fallback_model_id = fallback_model_id
        cls._adaptive_reserve = adaptive_reserve
        cls._stream_max_retries = stream_max_retries

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
    ):
        # 如果传入了手动参数，注册为 "main" 配置
        if api_key:
            if not base_url or not model:
                raise ValueError(
                    "手动构造 LLMService 需同时提供 base_url 和 model"
                    "（或使用装配根 container 装配）"
                )
            ClientManager.register_config(
                "main",
                api_key=api_key,
                base_url=base_url,
                model=model,
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

        fallback_fn = _build_fallback_fn(kwargs, model_key)

        # client 由 ClientManager 连接池按 key 缓存复用，与整流 attempt 无关，
        # 提到循环外，避免循环内定义闭包引用循环变量（Pylance 警告）。
        client = ClientManager.get_client(model_key)

        # 准备重试执行器 + fallback 函数 + 日志记录（辅助函数统一构建）
        retry = RetryHandlerManager.get(model_key)

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
        adaptive = self._adaptive_reserve
        prompt_tokens = 0
        estimated = 0
        if adaptive:
            prompt_tokens = _count_prompt_tokens(
                model_key, messages
            )  # max_tokens=0 → prompt+2
        else:
            estimated = _count_prompt_tokens(model_key, messages, max_tokens)
        limiter = ReservationLimiterManager.get(model_key)

        # ----- 流式整流重试（独立策略 StreamingRectifier） -----
        # 首 token 前中断可整流重试，已产出 token 后中断放弃。
        # create 阶段由 retry.execute() 保护（重试/熔断/fallback），
        # 迭代阶段异常由 rectifier 判断整流。产出 SSE 事件字符串。

        if result is None:
            result = StreamResult()
        # 当前活跃 reservation，跨 create 与迭代传递。定义在整流循环外：
        # 循环内各 attempt 的迭代分支与 call_fn 需要读写同一个 dict。
        active: dict[str, Reservation] = {}
        event_fields = _build_event_fields(
            model_key, messages, temperature, bool(tools), stream=True
        )
        rectifier_context = RectifierContext(result, active, event_fields)

        async for event in StreamingRectifier.rectified_stream(
            create_fn=lambda: _rate_limited_call(
                adaptive,
                limiter,
                client,
                kwargs,
                active,
                prompt_tokens=prompt_tokens,
                estimated=estimated,
                max_tokens=max_tokens,
            ),
            retry=retry,
            cancel_event=cancel_event,
            stream_max_retries=self._stream_max_retries,
            context=rectifier_context,
            fallback_fn=fallback_fn,
        ):
            yield event

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
            StreamResult | None（可恢复失败返回 None）

        Raises:
            不可恢复错误（4xx/认证/熔断开启）：重试/降级无意义，向上抛。
            可恢复错误（超时/5xx/429）重试耗尽后返回 None。
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

        fallback_fn = _build_fallback_fn(kwargs, model_key)

        client = ClientManager.get_client(model_key)

        retry = RetryHandlerManager.get(model_key)

        # 客户端限流：reserve/settle 统一闭环（与 async_generate 一致）。
        # 每次真实请求（含 retry 内部重试）都重新 reserve，create 失败全额退。
        # 自适应预留：开启 llm_adaptive_reserve 时用高分位估算输出（结构性解耦）。
        adaptive = self._adaptive_reserve
        prompt_tokens = 0
        estimated = 0
        if adaptive:
            prompt_tokens = _count_prompt_tokens(
                model_key, messages
            )  # max_tokens=0 → prompt+2
        else:
            estimated = _count_prompt_tokens(model_key, messages, max_tokens)
        limiter = ReservationLimiterManager.get(model_key)

        # 当前活跃 reservation（跨 create 与结算传递）。generate 无整流循环，
        # 但 retry 内部重试会多次调用 call_fn，需用同一 dict 让成功路径读到。
        active: dict[str, Reservation] = {}

        event_fields = _build_event_fields(
            model_key, messages, temperature, bool(tools), stream=False
        )

        start_time = time.monotonic()

        try:
            response = await retry.execute(
                call_fn=lambda: _rate_limited_call(
                    adaptive,
                    limiter,
                    client,
                    kwargs,
                    active,
                    prompt_tokens=prompt_tokens,
                    estimated=estimated,
                    max_tokens=max_tokens,
                ),
                fallback_fn=fallback_fn,
            )
        except Exception as e:
            await fill_llm_event_fields(
                event_fields,
                success=False,
                error=str(e)[:200],
                duration=time.monotonic() - start_time,
            )
            # 契约：可恢复错误（超时/5xx/429）可靠性层已重试耗尽 → 返回 None
            # （调用方按「业务无结果」降级）；不可恢复错误（4xx/认证/熔断开启）
            # 是调用方问题或下游拒绝，降级无意义 → 向上抛让调用方感知并决策。
            if classify_error(e) == ErrorCategory.NON_RETRYABLE:
                raise
            return None

        # 解析非流式响应 + 结算退差（LLM-002：try/finally 兜底，与流式
        # rectified_stream 的 finally 对齐——解析失败 / settle 被取消时不泄漏配额）。
        # 正常：finally 内 settle(actual) 退 TPM 差；
        # 解析抛异常：sr.usage 为 None → settle(None) 保留全部预留 + 标记终态
        #   （请求已发出，RPM/TPM 是真实消耗，不 cancel 全额退）；
        # settle 被硬取消：未终态 res cancel() 全额退 + re-raise（不吞取消信号）。
        sr = StreamResult()
        try:
            parsed = StreamParser.parse_non_stream(response)
            sr.content = parsed.get("content", "")
            sr.finish_reason = parsed.get("finish_reason")
            sr.tool_calls = parsed.get("tool_calls", [])
            sr.usage = parsed.get("usage")
            sr.refusal = parsed.get("refusal")
        finally:
            res = active.pop("res", None)
            if res is not None and not res.settled:
                try:
                    await res.settle((sr.usage or {}).get("total_tokens"))
                except BaseException:
                    # settle(actual) 被取消 → 未终态 res 收尾（LLM-003）：请求已发出，
                    # settle(None) 保留配额 + 标记终态（不 cancel 退 RPM，防配额虚增→429）
                    if not res.settled:
                        await res.settle(None)
                    raise

        await fill_llm_event_fields(
            event_fields,
            success=True,
            error=None,
            duration=time.monotonic() - start_time,
            usage=sr.usage,
            finish_reason=sr.finish_reason,
        )

        return sr

    async def generate_structured(
        self,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
        max_tokens: int | None = None,
    ) -> dict | None:
        """
        生成结构化输出（委托 StructuredOutput.extract 三级降级）。

        能力：JSON Schema(strict) → JSON Mode → 正则提取，逐级降级。

        Args:
            messages: 消息列表
            schema: JSON Schema
            model_key: 模型标识（默认 fast）
            max_tokens: 输出预算上限。None 用 settings.llm_structured_max_tokens
                （默认 2048）；截断时扩 2 倍重试 1 次。

        Returns:
            解析后的 dict，失败返回 None

        Raises:
            StructuredRefusalError: 模型拒答（内容安全策略触发）。调用方需区分
                「三级耗尽返回 None」与「拒答」——拒答通常需要差异化处理。
            StructuredToolCallError: 模型选择调用工具而非输出 JSON（finish_reason=
                tool_calls）。降级无意义，短路抛给调用方按工具调用处理。
        """
        return await StructuredOutput.extract(
            llm_service=self,
            messages=messages,
            schema=schema,
            model_key=model_key,
            max_tokens=max_tokens,
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
