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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

from app.domain.ports.llm_gateway import StreamResult
from app.platform.observability.logger import fill_llm_event_fields
from app.shared.exceptions import LLMCancelledError, LLMDeadlineExceededError
from app.shared.types import Messages

if TYPE_CHECKING:
    # 注解-only：llm_service 不直接依赖 openai 运行时（client 由 .client 子模块管理），
    # `from __future__ import annotations` 下注解不求值，守卫即可。
    from openai import AsyncOpenAI

    from .retry import RetryHandler

# 包内组件一律相对深路径 import（LLM 包对外只暴露 LLMService，__init__ 不重导出内部组件）
from .client import ClientManager
from .cost_tracker import CostTracker
from .errors import (
    _ExecutionAbort,
    decide_downstream_error,
    translate_abort,
)
from .execution_control import _raise_if_aborted, await_with_execution_control
from .request_budget import RequestBudgetGuard, RequestBudgetManager
from .reservation_limiter import (
    Reservation,
    ReservationLimiter,
    ReservationLimiterManager,
)
from .retry import RetryHandlerManager
from .streaming import StreamParser
from .streaming_rectifier import RectifierContext, StreamingRectifier
from .structured import StructuredOutput
from .token_counter import TiktokenTokenCounter

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


@dataclass(frozen=True)
class _CallContext:
    """一次 LLM 调用内已就绪的请求上下文（主 / fallback / 续接三闭包共享）。

    真实请求前后恒定的请求件在此一次性装配：client（连接池缓存）、限流预留策略
    （adaptive + token 估算，整流 / 重试循环外一次算好）、跨闭包共享的结算容器
    ``active`` 与业务取消信号。``active`` 是可变 dict（frozen 只防字段被替换）；
    budget_guard / limiter 按 guard_key 各异、不进本对象——各闭包在真实请求时经
    Manager 解析（fallback 独立键窗口 + 独立配额池），保持每次真实调用重新 reserve。
    """

    client: AsyncOpenAI
    active: dict[str, Reservation]
    adaptive: bool
    prompt_tokens: int
    estimated: int
    max_tokens: int
    cancel_event: asyncio.Event | None = None
    # LLM-044：整体执行期限（monotonic 绝对时刻，调用方现算）。真实请求三检查点 /
    # reserve / create 执行控制均按 ctx 同一信号判断——不逐层重计、不重算时长。
    deadline: float | None = None


async def _budget_guarded_call(
    budget_guard: RequestBudgetGuard,
    limiter: ReservationLimiter,
    ctx: _CallContext,
    kwargs: dict[str, Any],
) -> Any:
    """真实请求入口：请求预算准入 → 限流闭环（含取消复查） → provider 调用。

    每次真实调用（整流 attempt / 半流续接 / 非流式重试 / fallback 备用链路）都经
    本入口。client / 预留策略 / 结算容器 ``ctx.active`` / 取消复查由 ``_CallContext``
    承载（一次调用内共享），budget_guard / limiter 由调用点按 guard_key 解析传入。
    准入在同一入口内按序执行、职责分明：
    1. **入口执行检查**：cancel_event 置位 / 绝对 deadline 到期 → 抛类型化终止信号
       （`_StreamCancel` / `_DeadlineExceeded`），不发本请求（也不进预算/限流队列）。
    2. **预算闸**：按最终请求参数校验窗口，超限请求不预留配额、不触网络，抛
       `ContextWindowExceededError`（不可重试）。
    3. **限流闭环（受控 reserve）**：reserve 排队等待经执行控制（LLM-044），可被
       cancel/deadline 中断（部分配额经 R5 循环退款回收）；每次真实请求都重新 reserve。

    4. **reserve 后、create 前复查**：覆盖「配额已取得但外层同时取消 / 迟回」竞态——
       create 尚未启动，命中 cancel/deadline 则 `cancel()` 全额退 + 不发起请求
       （LLM-044/045：reserve 与终止同时完成、或吞取消以值迟回时，Reservation 已被
       helper 返回，此处显式释放，不丢返回值）。
    5. **create 状态机（create_started，LLM-044 / LLM-045）**：create task 一旦调度，
       请求可能已到达 provider。终止到达后按 create 结局分派：
       - create **配合取消**（重抛 CancelledError）或外层硬取消 → 此处 `settle(None)`
         保守结算（防配额虚增→429，不 cancel 全额退）；
       - create **吞取消以值迟回**（response / stream 实际已取得）→ 正常 return，
         Reservation 留在 ``ctx.active`` 交调用方（generate/整流）接管——按实际 usage
         `settle(actual)` 或整流器结算，不再于本步 settle(None)；
        - create 自然抛的普通传输异常 → 请求是否到达 provider 未知，`settle(None)`
          保守关闭预留责任；retry 是否继续由可靠性层独立决定。

    预算不混入限流步骤内部：它是入口的独立第一步，超限异常在网络前上抛，由
    整流器/领域层终结。

    fallback 备用链路同样经本入口（fallback 键窗口 + 独立配额池）。
    """
    # ----- ① 入口执行检查（先于预算/限流）：已取消/已到期 → 不发本请求 -----
    _raise_if_aborted(ctx.cancel_event, ctx.deadline)

    # ----- ② 请求预算准入（独立步骤，先于 reserve） -----
    budget_guard.validate(kwargs["model"], kwargs)

    # ----- ③ 限流闭环：reserve 受执行控制等待（LLM-044，可中断排队） -----
    if ctx.adaptive:
        res = await await_with_execution_control(
            lambda: limiter.reserve_adaptive(
                prompt_tokens=ctx.prompt_tokens, max_tokens=ctx.max_tokens
            ),
            cancel_event=ctx.cancel_event,
            deadline=ctx.deadline,
        )
    else:
        res = await await_with_execution_control(
            lambda: limiter.reserve(estimated_tokens=ctx.estimated),
            cancel_event=ctx.cancel_event,
            deadline=ctx.deadline,
        )
    ctx.active["res"] = res

    # ----- ④ reserve 后、create 前复查（create 尚未启动 → cancel 全额退） -----
    # 竞态 / 迟回：reserve 与终止同时完成、或 reserve 吞取消以值迟回时，Reservation 已
    # 被 helper 返回并落入 active，此处显式释放（LLM-044/045：不丢返回值）。
    try:
        _raise_if_aborted(ctx.cancel_event, ctx.deadline)
    except _ExecutionAbort:  # create 未启动即命中终止：请求未发
        await res.cancel()  # 全额退（RPM 1 + TPM 全额）
        ctx.active.pop("res", None)
        raise

    # ----- ⑤ create 受执行控制（create_started 状态机，LLM-044/045） -----
    # 吞取消迟回值（create 实际成功、以值返回）不经此处 except，直接 return 交下游接管；
    # 仅 create 配合取消 / 清理期异常 / 外层硬取消 / 普通传输异常落下方 except 收尾。
    try:
        return await await_with_execution_control(
            lambda: ctx.client.chat.completions.create(**kwargs),
            cancel_event=ctx.cancel_event,
            deadline=ctx.deadline,
        )
    except _ExecutionAbort:  # 业务取消 / deadline
        await res.settle(None)  # 保留全部预留（不退 RPM/TPM，防配额虚增→429）
        ctx.active.pop("res", None)
        raise
    except asyncio.CancelledError:  # 外层硬取消 CancelledError
        await res.settle(None)  # 保留全部预留（不退 RPM/TPM，防配额虚增→429）
        ctx.active.pop("res", None)
        raise
    except BaseException:  # create 已调度：普通传输异常也不能证明请求未到 provider
        await res.settle(None)  # 保留预留，避免本地配额被未知远端执行虚增
        ctx.active.pop("res", None)
        raise


# =====================================================================
# 编排产物（async_generate / generate 共用）
# =====================================================================


class _RequestPlan(NamedTuple):
    """一次 LLM 调用（流式 / 非流式共用）的编排产物。

    ``active`` 为跨主链路 / fallback / 续接共享的 Reservation 结算容器：成功路径
    settle、失败 / 取消路径 cancel，均由调用方统一收尾。三个真实请求闭包每次调用
    都经单一入口 ``_budget_guarded_call``（预算准入 → reserve → 取消复查 → create）。
    """

    retry: RetryHandler
    active: dict[str, Reservation]
    ctx: _CallContext
    call_fn: Callable[[], Awaitable[Any]]
    fallback_fn: Callable[[], Awaitable[Any]] | None
    continue_fn: Callable[[str], Awaitable[Any]] | None
    event_fields: dict[str, Any]


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
    # 私有编排（async_generate / generate 共用）
    # ==================================================================

    def _plan_request(
        self,
        *,
        model_key: str,
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float,
        max_tokens: int,
        stream: bool,
        response_format: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> _RequestPlan:
        """一次 LLM 调用的公共编排（流式 / 非流式通道共用）。

        从方法原始参数到整套请求件的统一装配：
        ① 用 `_build_chat_kwargs` 组装 provider 请求 kwargs（stream 时补 include_usage）
        ② 按 model_key 解析 client / retry （连接聚合 / 熔断观察面，主 / 副 / 续接共用，fallback 纯兜底不入熔断）
        ③ TPM预留量在整流 / 重试循环外一次估算（自适应形态走高分位、固定形态把 max_tokens 并入估算）
        ④ ``active`` 作为跨主 / 副 / 续接共享的结算容器
        ⑤ 内联构造三个真实请求闭包：主 ``call_fn``（保留 kwargs 已装配的主模型）、启用备用
        模型时的 ``fallback_fn``（覆盖 ``kwargs["model"]`` 为备用模型，"fallback" 独立键窗口
        + 独立配额池，复用主 client，LLM-012 仅约束 base_url/密钥复用）与半流续接
        ``continue_fn``（LLM-ADR-015，仅流式且配置开启）。各闭包每次调用按各自 guard_key
        解析预算闸与限流器后走单一入口 ``_budget_guarded_call``（预算准入 → reserve →
        取消复查 → create），与整流 attempt / retry 重试共生命同期，Reservation 落入同一
        ``active`` 由调用方在通道尾部统一 settle / cancel。

        请求组装（构建 kwargs / 估算 / 闭包构造）在整流与 retry 的 try 之
        外 fail fast：配置错误（未注册 key 等）自然上抛，不被通道级异常处理吞掉。
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

        # 已就绪请求上下文：client/预留策略/active/cancel 一次装配，主/副/续接三闭包共享
        # （client 按连接聚合、retry 按熔断观察面，均主键单例；仅窗口/配额按 guard_key 分键）。
        client = ClientManager.get_client(model_key)
        retry = RetryHandlerManager.get(model_key)

        # TPM 预留量估算：委托 TiktokenTokenCounter（计数口径单一事实源，见 token_counter.py），
        # 一次构建 encoder。自适应形态走高分位预留（prompt/max_tokens 分传，输出余量不并入）；
        # 固定形态把 max_tokens 并入单值估算（TPM 桶按"请求可能消耗的最大 token"扣减）。
        counter = TiktokenTokenCounter(ClientManager.get_model(model_key))
        prompt_count = counter.count_messages_tokens(messages)
        adaptive = self._adaptive_reserve
        if adaptive:
            prompt_tokens = prompt_count
            estimated = 0
        else:
            prompt_tokens = 0
            estimated = prompt_count + max_tokens

        active: dict[str, Reservation] = {}

        event_fields = _build_event_fields(
            model_key, messages, temperature, bool(tools), stream=stream
        )

        ctx = _CallContext(
            client=client,
            active=active,
            adaptive=adaptive,
            prompt_tokens=prompt_tokens,
            estimated=estimated,
            max_tokens=max_tokens,
            cancel_event=cancel_event,
            deadline=deadline,
        )

        # 主链路：保留 kwargs 已装配的主模型（guard_key = 调用方 model_key）。主 / 副 / 续接
        # 闭包均为「普通函数返回预算入口协程」：retry.execute / 整流器只 await 一次即执行，
        # async 再包一层会留下未运行的内层协程（provider 永不调用）。
        def call_fn() -> Awaitable[Any]:
            return _budget_guarded_call(
                RequestBudgetManager.get(model_key),
                ReservationLimiterManager.get(model_key),
                ctx,
                kwargs,
            )

        # fallback 备用链路：仅启用备用模型时构造；覆盖 kwargs["model"] 为备用模型，走
        # 独立 "fallback" 键窗口 + 独立配额池（复用主 client，LLM-012；同端点 ≠ 同窗口，
        # 详见 issues/integration/llm/2026-09-06-fallback-window-and-quota）。
        fallback_fn = None
        if self._fallback_model_id:
            fb_kwargs = {**kwargs, "model": self._fallback_model_id}

            def fallback_fn() -> Awaitable[Any]:
                return _budget_guarded_call(
                    RequestBudgetManager.get("fallback"),
                    ReservationLimiterManager.get("fallback"),
                    ctx,
                    fb_kwargs,
                )

        # 半流续接：仅流式且配置开启（>0）；prefix 追加为 assistant 消息续写（LLM-ADR-015，
        # OpenAI 兼容端点不支持 prefix 时 create 失败，整流器尽力而为降级到放弃）。
        continue_fn = None
        if stream and self._continuation_max_retries > 0:

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
        # 公共编排（见 _plan_request）：build kwargs / 估算 / 主副/续接闭包一次就绪
        plan = self._plan_request(
            model_key=model_key,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            cancel_event=cancel_event,
            deadline=deadline,
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
            async for event in StreamingRectifier.rectified_stream(
                create_fn=plan.call_fn,
                retry=plan.retry,
                cancel_event=cancel_event,
                stream_max_retries=self._stream_max_retries,
                context=rectifier_context,
                fallback_fn=plan.fallback_fn,
                continue_fn=plan.continue_fn,
                continuation_max_retries=self._continuation_max_retries,
                deadline=deadline,
            ):
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
        # 公共编排（见 _plan_request）：build kwargs / 估算 / 主副闭包一次就绪。
        # 绑定本地名，使下方重试 / 解析 / 结算收尾与编排字段一一对应。
        plan = self._plan_request(
            model_key=model_key,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            response_format=response_format,
            cancel_event=cancel_event,
            deadline=deadline,
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
                    await res.settle(
                        (sr.usage or {}).get("total_tokens")
                    )  # 按实际 usage 退 TPM 差（RPM 不退）
                except BaseException:
                    # settle(actual) 被取消 → 未终态 res 收尾（LLM-003）：请求已发出，
                    if not res.settled:
                        await res.settle(
                            None
                        )  # 保留全部预留并标记终态（不 cancel，防配额虚增）
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
