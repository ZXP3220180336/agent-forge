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
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, ClassVar

from app.domain.ports.llm_gateway import StreamResult
from app.platform.observability.logger import fill_llm_event_fields
from app.shared.exceptions import LLMCancelledError, LLMDeadlineExceededError
from app.shared.types import Messages

# 包内组件一律相对深路径 import（LLM 包对外只暴露 LLMService，__init__ 不重导出内部组件）
from .client import ClientManager
from .cost_tracker import CostTracker
from .errors import (
    _ExecutionAbort,
    decide_downstream_error,
    translate_abort,
)
from .request_execution import build_request_plan
from .streaming import StreamParser
from .streaming_rectifier import RectifierContext, StreamingRectifier
from .structured import StructuredOutput
from .token_counter import TiktokenTokenCounter

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
    # 半流续接轮次上限（LLM-ADR-015）：已产出 content 中断时带前缀续写；0=禁用
    _continuation_max_retries: ClassVar[int] = 0

    @classmethod
    def register_config(
        cls,
        *,
        fallback_model_id: str,
        adaptive_reserve: bool,
        stream_max_retries: int,
        continuation_max_retries: int = 0,
    ) -> None:
        """注入运行期配置（由装配根调用，避免直接依赖 settings）。"""
        cls._fallback_model_id = fallback_model_id
        cls._adaptive_reserve = adaptive_reserve
        cls._stream_max_retries = stream_max_retries
        cls._continuation_max_retries = continuation_max_retries

    def __init__(
        self,
        api_key: str = "",
        model: str = "",
        base_url: str = "",
    ):
        # 惰性主模型 token 计数器（count_tokens/count_messages_tokens 首次调用时构建）
        self._counter: TiktokenTokenCounter | None = None
        # 如果传入了手动参数，注册为 "main" 配置
        if api_key:
            if not base_url or not model:
                raise ValueError("手动构造 LLMService 需同时提供 base_url 和 model（或使用装配根 container 装配）")
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
        deadline: float | None = None,
    ) -> AsyncGenerator[str]:
        """
        单轮 LLM 流式生成（Agent 专用）。

        签名向后兼容，新增 model_key / cancel_event 可选参数。

        Args:
            model_key: 使用 ClientManager 的哪个配置（main / reasoning / fast）
            cancel_event: 取消信号，置位时优雅终止
            deadline: 整体执行期限（monotonic 绝对，由调用方现算）；整流 create /
                reserve / 读取期按同一期限受控（LLM-044）

        Yields:
            str: SSE 事件字符串

        Raises:
            ContextWindowExceededError: 请求预算闸在网络调用前拒绝（业务边界短路）。
            LLMCancelledError: ``cancel_event`` 命中；完成当前阶段必要资源收尾后类型化
                冒泡，不生成取消 SSE，也不写 ``result.error``。已产内容、reasoning 与
                usage 保留在调用方传入的 ``result``，异常同时携带可得 usage。
            LLMDeadlineExceededError: 整体期限耗尽（含整流读取期命中）；类型化冒泡，
                不折为普通 error 事件。其余流式失败折为 error 事件产出，不抛异常。
            asyncio.CancelledError: 外部 task 硬取消；完成 ``finally`` 资源兜底后原样传播。
        """
        # 公共编排（见 build_request_plan）：build kwargs / 估算 / 主副/续接闭包一次就绪
        plan = build_request_plan(
            model_key=model_key,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            cancel_event=cancel_event,
            deadline=deadline,
            fallback_model_id=self._fallback_model_id,
            adaptive_reserve=self._adaptive_reserve,
            continuation_max_retries=self._continuation_max_retries,
        )
        if result is None:
            result = StreamResult()
        rectifier_context = RectifierContext(result, plan.active, plan.event_fields)

        # ----- 流式整流 / 续接（独立策略 StreamingRectifier） -----
        # 首 token 前中断 → 整流重试；已产出 content 中断 → 续接（尽力而为）或放弃。
        # create 阶段由 plan.retry.execute() 保护（重试/熔断/fallback），
        # 迭代阶段异常由 rectifier 判断整流/续接。产出 SSE 事件字符串。
        # 整流内部私有执行终止信号（业务取消 / deadline，LLM-044/C-12）在 Facade 边界翻译为
        # shared 领域出口抛给领域层（domain 不依赖 integration 私有异常）；其余流式
        # 失败已折为 error 事件 / result.error，不抛异常。
        try:
            rectified_events = StreamingRectifier.rectified_stream(
                create_fn=plan.call_fn,
                retry=plan.retry,
                cancel_event=cancel_event,
                stream_max_retries=self._stream_max_retries,
                context=rectifier_context,
                fallback_fn=plan.fallback_fn,
                continue_fn=plan.continue_fn,
                continuation_max_retries=self._continuation_max_retries,
                deadline=deadline,
            )
            async with aclosing(rectified_events):
                async for event in rectified_events:
                    yield event
        except _ExecutionAbort as e:
            raise translate_abort(e) from None

    async def generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0,
        max_tokens: int = 1024,
        response_format: dict | None = None,
        model_key: str = "fast",
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> StreamResult | None:
        """
        非流式单轮生成（适合简单任务）。

        Args:
            model_key: 使用哪个模型（默认 fast，低成本）
            response_format: 结构化输出格式，如 {"type": "json_object"}
            cancel_event: 业务取消信号（LLM-044）：真实请求入口/reserve/create/
                返回前检查点受控，置位即优雅终止
            deadline: 整体执行期限（monotonic 绝对，LLM-044）：同一期限约束每笔
                reserve/create 与结算后复查

        Returns:
            StreamResult | None（可恢复失败返回 None）

        Raises:
            不可恢复错误（4xx/认证/熔断开启）：重试/降级无意义，向上抛。
            可恢复错误（超时/5xx/429）重试耗尽后返回 None。
            LLMCancelledError / LLMDeadlineExceededError：执行控制终止（用户取消 /
                整体期限耗尽）——Facade 把内部私有信号翻译为本异常上抛，领域据此
                按取消/超时收尾，而非当失败重试。
        """
        # 公共编排（见 build_request_plan）：build kwargs / 估算 / 主副闭包一次就绪。
        # 绑定本地名，使下方重试 / 解析 / 结算收尾与编排字段一一对应。
        plan = build_request_plan(
            model_key=model_key,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            response_format=response_format,
            cancel_event=cancel_event,
            deadline=deadline,
            fallback_model_id=self._fallback_model_id,
            adaptive_reserve=self._adaptive_reserve,
            continuation_max_retries=self._continuation_max_retries,
        )
        event_fields = plan.event_fields
        active = plan.active
        retry = plan.retry

        start_time = time.monotonic()

        try:
            response = await retry.execute(
                call_fn=plan.call_fn,
                fallback_fn=plan.fallback_fn,
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except _ExecutionAbort as e:
            # LLM-044 Facade 出口：私有执行终止信号（取消/期限）翻译为 shared 领域异常，
            # 不折为可恢复失败 None / 不上抛私有类型（domain 不得依赖 integration 私有）。
            raise translate_abort(e) from None
        except Exception as e:
            await fill_llm_event_fields(
                event_fields,
                success=False,
                error=str(e)[:200],
                duration=time.monotonic() - start_time,
            )
            # 统一决策（llm/errors.py）：
            # 可恢复错误（超时/5xx/429）可靠性层已重试耗尽 → 降级 return None（业务无结果）；
            # openai 不可恢复错误（4xx/认证）归一为 LLMAPIError（AppError 树）上抛 → 领域层 except AppError 统一兜底（REASON-010 闭环）；
            # 非 openai 异常（CircuitBreakerOpenError / 编程错误）原样 re-raise 保留 traceback。
            decision = decide_downstream_error(e)
            if decision.to_raise is None:
                return None
            if decision.normalized:
                raise decision.to_raise from e
            raise

        # 解析非流式响应 + 结算退差（LLM-002：try/finally 兜底，与流式
        # rectified_stream 的 finally 对齐——解析失败 / settle 被取消时不泄漏配额）。
        # 正常：finally 内 settle(actual) 退 TPM 差；
        # 解析抛异常：sr.usage 为 None → settle(None) 保留全部预留 + 标记终态
        #   （请求已发出，RPM/TPM 是真实消耗，不 cancel 全额退）；
        # settle 被硬取消：未终态 res 以 settle(None) 保守关闭 + re-raise（不吞取消信号）。
        sr = StreamResult()
        try:
            parsed = StreamParser.parse_non_stream(response)
            sr.content = parsed.get("content", "")
            sr.finish_reason = parsed.get("finish_reason")
            sr.tool_calls = parsed.get("tool_calls", [])
            sr.usage = parsed.get("usage")
            sr.refusal = parsed.get("refusal")
            sr.reasoning_content = parsed.get("reasoning_content", "")
            sr.has_reasoning = parsed.get("has_reasoning", False)
        finally:
            res = active.pop("res", None)
            if res is not None and not res.settled:
                try:
                    await res.settle((sr.usage or {}).get("total_tokens"))  # 按实际 usage 退 TPM 差（RPM 不退）
                except BaseException:
                    # settle(actual) 被取消 → 未终态 res 收尾（LLM-003）：请求已发出，
                    if not res.settled:
                        await res.settle(None)  # 保留全部预留并标记终态（不 cancel，防配额虚增）
                    raise

        # 非关键观测先在自己的有界 best-effort 边界内完成；最终 Guard 之后到 return
        # 不再存在可阻塞 await，避免日志等待期间新到的取消/deadline 穿过提交窗口。
        await fill_llm_event_fields(
            event_fields,
            success=True,
            error=None,
            duration=time.monotonic() - start_time,
            usage=sr.usage,
            finish_reason=sr.finish_reason,
        )

        # LLM-044 检查点 3：create 成功返回、结算与观测完成后，返回前复查执行状态——当前
        # 请求在执行期间已到期/被取消 → 抛 shared 终止（不把「恰好完成但已超时」的
        # 结果当成功返回；费用已 settle、信号携带 sr.usage 供上层成本归量不丢）。
        # generate 是 Facade 边界：直接抛 shared 领域异常（域不依赖 integration 私有）。
        if plan.ctx.cancel_event is not None and plan.ctx.cancel_event.is_set():
            raise LLMCancelledError(message="用户取消", usage=sr.usage)
        if plan.ctx.deadline is not None and time.monotonic() >= plan.ctx.deadline:
            raise LLMDeadlineExceededError(message="执行期限耗尽", usage=sr.usage)

        return sr

    async def generate_structured(
        self,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
        max_tokens: int | None = None,
        usage: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
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
            usage: 可选，可变引用回填本次 extract 全程调用的 token 用量累计
                （prompt_tokens / completion_tokens / total_tokens，含多级降级 /
                截断重试 / 回喂的所有成功调用，供成本计量）。
                仿 ``async_generate`` 的 ``result`` 参数模式——返回签名不变，向后兼容。
            cancel_event: 业务取消信号；已置位则降级链不再发起后续子调用，返回 None
                （与降级耗尽同出口）。
            deadline: 绝对截止时刻（time.monotonic），由调用方现算（同一时间预算不
                逐级重计）；已过则同上拦截。None = 不限制。

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
            usage=usage,
            cancel_event=cancel_event,
            deadline=deadline,
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

    def count_tokens(self, text: str) -> int:
        """计算单段文本 token 数（主模型编码，委托 tiktoken）。"""
        return self._token_counter().count_tokens(text)

    def count_messages_tokens(self, messages: Messages) -> int:
        """计算 messages 列表总 token 数（含格式开销，主模型编码）。"""
        return self._token_counter().count_messages_tokens(messages)

    def _token_counter(self) -> TiktokenTokenCounter:
        """惰性构建主模型 tiktoken 计数器（计数方法经 LLMGateway 端口对外）。"""
        if self._counter is None:
            self._counter = TiktokenTokenCounter(ClientManager.get_model("main"))
        return self._counter
