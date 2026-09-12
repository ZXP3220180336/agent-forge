"""预算闸跨 Facade / 真实重试器 / 整流器 / 结构化输出的边界回归。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.reasoning import ReActStrategy
from app.integration.llm.client import ClientManager
from app.integration.llm.llm_service import LLMService
from app.integration.llm.request_budget import RequestBudgetConfig, RequestBudgetManager
from app.integration.llm.reservation_limiter import (
    ReservationLimiter,
    ReservationLimiterManager,
)
from app.integration.llm.retry import CircuitState, RetryConfig, RetryHandler, RetryHandlerManager
from app.integration.llm.streaming_rectifier import StreamingRectifier
from app.shared.exceptions import (
    ContextWindowExceededError,
    LLMCancelledError,
    LLMDeadlineExceededError,
)


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr(RequestBudgetManager, "_configs", {})
    monkeypatch.setattr(RequestBudgetManager, "_instances", {})
    RequestBudgetManager.register_config({
        "fast": RequestBudgetConfig(1000, 16),
        "fallback": RequestBudgetConfig(300, 16),
    })
    retry = RetryHandler(RetryConfig(max_retries=0))
    create = AsyncMock()
    limiter = SimpleNamespace(reserve=AsyncMock(), reserve_adaptive=AsyncMock())
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ClientManager, "get_model", lambda key: "gpt-4")
    monkeypatch.setattr(ClientManager, "get_client", lambda key: client)
    monkeypatch.setattr(RetryHandlerManager, "get", lambda key: retry)
    monkeypatch.setattr(ReservationLimiterManager, "get", lambda key: limiter)
    monkeypatch.setattr(LLMService, "_fallback_model_id", "")
    monkeypatch.setattr(LLMService, "_adaptive_reserve", False)
    return SimpleNamespace(
        service=LLMService(),
        retry=retry,
        create=create,
        limiter=limiter,
        client=client,
    )


class _CancelTrackingReservation:
    """记录 cancel/settle 的 Reservation 桩（终态语义对齐真实实现）。"""

    def __init__(self) -> None:
        self.settled = False
        self.cancel_calls = 0
        self.settle_calls = 0
        self.last_actual = None

    async def settle(self, actual=None):
        self.settle_calls += 1
        self.settled = True
        self.last_actual = actual

    async def cancel(self):
        self.cancel_calls += 1
        self.settled = True


class _ScriptedBoundaryStream:
    """按给定 chunk 产出，并可在 EOF 位置抛一次异常的流桩。"""

    def __init__(self, chunks, *, error=None):
        self._chunks = iter(chunks)
        self._error = error
        self._raised = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            if self._error is not None and not self._raised:
                self._raised = True
                raise self._error
            raise StopAsyncIteration

    async def close(self):
        return None


class _HangingBoundaryStream:
    """读取永久挂起；close 可正常延迟完成或等待硬取消。"""

    def __init__(self, order, *, close_delay=0.0, hang_on_close=False):
        self.order = order
        self.close_delay = close_delay
        self.hang_on_close = hang_on_close
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()

    async def close(self):
        self.close_calls += 1
        self.order.append("close_started")
        try:
            if self.hang_on_close:
                await asyncio.Event().wait()
            elif self.close_delay:
                await asyncio.sleep(self.close_delay)
        except asyncio.CancelledError:
            self.order.append("close_cancelled")
            raise
        self.order.append("close")


class _OrderedReservation(_CancelTrackingReservation):
    """记录 settle 与资源关闭的相对顺序。"""

    def __init__(self, order, *, settle_delay=0.0):
        super().__init__()
        self.order = order
        self.settle_delay = settle_delay

    async def settle(self, actual=None):
        if self.settle_delay:
            await asyncio.sleep(self.settle_delay)
        await super().settle(actual)
        self.order.append("settle")


def _boundary_content_chunk(text):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None,
                    content=text,
                    tool_calls=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _boundary_usage_chunk(prompt, completion):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            model_dump=lambda: {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
        ),
    )


def _open_circuit(boundary) -> None:
    """置熔断 OPEN 且冷却未过（_last_failure_time 指向未来）。

    仅置 ``_state = OPEN`` 不够：OPEN 超过 recovery_timeout 会自动转 HALF_OPEN
    并放行一个主链路探针（allow_request），并非真走 fallback——原「共享主窗口」
    用例正是因此实际测的是主链路预算闸而非 fallback 路径。
    """
    boundary.retry.circuit_breaker._state = CircuitState.OPEN
    boundary.retry.circuit_breaker._last_failure_time = time.monotonic() + 3600


def _install_fallback_waiting_limiter(monkeypatch, *, on_tpm_acquire=None):
    """安装真实 fallback limiter，并让 TPM acquire 停在可控排队点。"""
    limiter = ReservationLimiter(rpm=2, tpm=100)
    tpm_waiting = asyncio.Event()
    captured = {}

    async def _wait_for_tpm(tokens=1.0):
        captured["reserve_task"] = asyncio.current_task()
        tpm_waiting.set()
        if on_tpm_acquire is not None:
            await on_tpm_acquire()
        await asyncio.Event().wait()

    monkeypatch.setattr(limiter._token_bucket, "acquire", _wait_for_tpm)
    requested_keys = []

    def _get_limiter(key):
        requested_keys.append(key)
        assert key == "fallback"
        return limiter

    monkeypatch.setattr(ReservationLimiterManager, "get", _get_limiter)
    return limiter, tpm_waiting, captured, requested_keys


@pytest.mark.parametrize("channel", ["generate", "structured", "stream"])
async def test_rejection_preserves_channel_contract_and_has_no_side_effects(boundary, channel):
    messages = [{"role": "user", "content": "batch evidence " * 2000}]
    if channel == "stream":
        # 预算闸在网络调用前拒绝 → 异常穿透整流器上抛（非 error 事件折 LLM_FAILED）。
        with pytest.raises(ContextWindowExceededError):
            async for _ in boundary.service.async_generate(
                messages, model_key="fast", max_tokens=20,
            ):
                pass
    else:
        with pytest.raises(ContextWindowExceededError):
            if channel == "generate":
                await boundary.service.generate(messages, max_tokens=20)
            else:
                await boundary.service.generate_structured(messages, {"type": "object"}, max_tokens=20)
    boundary.create.assert_not_awaited()
    boundary.limiter.reserve.assert_not_awaited()
    boundary.limiter.reserve_adaptive.assert_not_awaited()
    assert boundary.retry.circuit_breaker.failure_count == 0


async def test_open_circuit_fallback_uses_fallback_window_guard(boundary, monkeypatch):
    """熔断 OPEN 走 fallback 时预算闸仍生效，且按 fallback 独立窗口校验。

    修复前：fallback 沿用主 model_key 窗口（同 provider 共享）；修复后：备用模型
    按目标（备用）模型窗口校验——窗口独立于端点复用（LLM-012 只约束 base_url/key）。
    """
    monkeypatch.setattr(LLMService, "_fallback_model_id", "backup-model")
    _open_circuit(boundary)
    with pytest.raises(ContextWindowExceededError) as error:
        await boundary.service.generate(
            [{"role": "user", "content": "batch evidence " * 2000}], max_tokens=20
        )
    assert error.value.model_key == "fallback"  # 携带 fallback 键，而非主 key
    boundary.create.assert_not_awaited()
    boundary.limiter.reserve.assert_not_awaited()


async def test_fallback_rejects_under_its_own_smaller_window(boundary, monkeypatch):
    """主窗可容纳、fallback 小窗拒绝：证明不沿用主窗口（正例对照）。

    修复前：fallback 预算沿用主 fast 大窗（50000）→ 放行并触网络/限流；修复后：
    按 fallback 独立小窗（300）拒绝，SDK 调用与限流预留均为 0。
    """
    monkeypatch.setattr(LLMService, "_fallback_model_id", "backup-model")
    RequestBudgetManager.register_config({
        "fast": RequestBudgetConfig(50_000, 16),  # 主窗很大，能容纳
        "fallback": RequestBudgetConfig(300, 16),  # fallback 窗小
    })
    _open_circuit(boundary)
    with pytest.raises(ContextWindowExceededError) as error:
        await boundary.service.generate(
            [{"role": "user", "content": "evidence text " * 200}], max_tokens=20
        )
    assert error.value.model_key == "fallback"
    boundary.create.assert_not_awaited()
    boundary.limiter.reserve.assert_not_awaited()


async def test_closed_fallback_budget_exceeded_raises_not_packaged(boundary, monkeypatch):
    """CLOSED 主链路网络失败→fallback 超限：上抛 ContextWindowExceededError 而非
    包成主网络错误（否则会被下游当可恢复错误触发再调主）。

    修复前：fallback 异常只作主异常 __cause__，主超时错误被 decide_downstream_error
    归 RETRYABLE → generate 返回 None 降级；修复后：终结性超限直抛。
    """
    monkeypatch.setattr(LLMService, "_fallback_model_id", "backup-model")
    RequestBudgetManager.register_config({
        "fast": RequestBudgetConfig(1_000, 16),  # 主窗：放行
        "fallback": RequestBudgetConfig(300, 16),  # fallback 窗：拒绝
    })
    boundary.create.side_effect = TimeoutError("main transport timeout")
    with pytest.raises(ContextWindowExceededError):
        await boundary.service.generate(
            [{"role": "user", "content": "evidence text " * 200}], max_tokens=20
        )
    # 主链路 1 次真实请求（重试 0）+ 该次 reserve；fallback 拒绝于网络/限流前
    assert boundary.create.await_count == 1
    assert boundary.limiter.reserve.await_count == 1


async def test_half_open_fallback_budget_exceeded_raises_not_packaged(boundary, monkeypatch):
    """HALF_OPEN 探针失败→fallback 超限：同 CLOSED，直抛不包主错误。"""
    monkeypatch.setattr(LLMService, "_fallback_model_id", "backup-model")
    RequestBudgetManager.register_config({
        "fast": RequestBudgetConfig(1_000, 16),
        "fallback": RequestBudgetConfig(300, 16),
    })
    boundary.create.side_effect = TimeoutError("main transport timeout")
    boundary.retry.circuit_breaker._state = CircuitState.HALF_OPEN
    boundary.retry.circuit_breaker._half_open_requests = 0
    with pytest.raises(ContextWindowExceededError):
        await boundary.service.generate(
            [{"role": "user", "content": "evidence text " * 200}], max_tokens=20
        )


@pytest.mark.parametrize(
    ("abort_kind", "expected_exception"),
    [
        ("cancel", LLMCancelledError),
        ("deadline", LLMDeadlineExceededError),
    ],
)
async def test_fallback_real_reserve_partial_acquire_aborts_and_refunds(
    boundary, monkeypatch, abort_kind, expected_exception
):
    """OPEN fallback 在 RPM 已扣、TPM 排队时响应终止并完成 R5 退款。"""
    monkeypatch.setattr(LLMService, "_fallback_model_id", "backup-model")
    _open_circuit(boundary)
    limiter, tpm_waiting, captured, requested_keys = _install_fallback_waiting_limiter(
        monkeypatch
    )
    cancel_event = asyncio.Event()
    deadline = time.monotonic() + 0.5 if abort_kind == "deadline" else None

    task = asyncio.create_task(
        boundary.service.generate(
            [{"role": "user", "content": "hi"}],
            model_key="fast",
            max_tokens=20,
            cancel_event=cancel_event,
            deadline=deadline,
        )
    )
    await asyncio.wait_for(tpm_waiting.wait(), timeout=1.0)
    if abort_kind == "cancel":
        cancel_event.set()

    with pytest.raises(expected_exception):
        await asyncio.wait_for(task, timeout=2.0)

    reserve_task = captured["reserve_task"]
    assert reserve_task is not None and reserve_task.done()
    assert requested_keys == ["fallback"]
    boundary.create.assert_not_awaited()
    assert limiter._req_bucket._tokens == pytest.approx(
        limiter._req_bucket.capacity
    )
    assert limiter._token_bucket._tokens == pytest.approx(
        limiter._token_bucket.capacity
    )
    assert boundary.retry.circuit_breaker.state == CircuitState.OPEN


async def test_fallback_r5_refund_survives_second_cancel(boundary, monkeypatch):
    """fallback reserve 的 RPM 退款再次被取消时，R5 循环仍补齐退款。"""
    monkeypatch.setattr(LLMService, "_fallback_model_id", "backup-model")
    _open_circuit(boundary)
    limiter, tpm_waiting, captured, _ = _install_fallback_waiting_limiter(
        monkeypatch
    )
    original_refund = limiter._req_bucket.refund
    refund_started = asyncio.Event()
    refund_attempts = 0

    async def _cancel_once_during_refund(tokens=1.0):
        nonlocal refund_attempts
        refund_attempts += 1
        if refund_attempts == 1:
            refund_started.set()
            await asyncio.Event().wait()
        await original_refund(tokens)

    monkeypatch.setattr(limiter._req_bucket, "refund", _cancel_once_during_refund)
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        boundary.service.generate(
            [{"role": "user", "content": "hi"}],
            model_key="fast",
            max_tokens=20,
            cancel_event=cancel_event,
        )
    )
    await asyncio.wait_for(tpm_waiting.wait(), timeout=1.0)
    cancel_event.set()
    await asyncio.wait_for(refund_started.wait(), timeout=1.0)
    reserve_task = captured["reserve_task"]
    assert reserve_task is not None
    reserve_task.cancel()

    with pytest.raises(LLMCancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert refund_attempts == 2
    assert reserve_task.done()
    assert limiter._req_bucket._tokens == pytest.approx(
        limiter._req_bucket.capacity
    )
    boundary.create.assert_not_awaited()
    assert boundary.retry.circuit_breaker.state == CircuitState.OPEN


async def test_stream_cancel_during_reserve_refunds_and_no_create(boundary, monkeypatch):
    """reserve 排队期间业务取消：退款 + 不再发起 SDK 请求（覆盖「配额已取得但外层
    已取消」竞态）。

    修复前：reserve 返回后无取消复查，仍照常 create（用户已取消却发出付费请求）；
    修复后：reserve 后 create 前复查 cancel_event，命中则 cancel 退款并走用户取消出口。
    """
    import asyncio as _asyncio

    monkeypatch.setattr(LLMService, "_fallback_model_id", "")
    RequestBudgetManager.register_config({
        "fast": RequestBudgetConfig(1000, 16),
        "fallback": RequestBudgetConfig(300, 16),
    })
    cancel_event = _asyncio.Event()
    res = _CancelTrackingReservation()

    class _ReserveThenCancel:
        """模拟 reserve 排队期间用户取消：返回前置位 cancel_event。"""

        async def reserve(self, estimated_tokens=0, retry_after=None):
            cancel_event.set()
            return res

        async def reserve_adaptive(self, prompt_tokens=0, max_tokens=0, retry_after=None):
            cancel_event.set()
            return res

    monkeypatch.setattr(ReservationLimiterManager, "get", lambda key: _ReserveThenCancel())

    events = []
    async for event in boundary.service.async_generate(
        [{"role": "user", "content": "hi"}],
        model_key="fast",
        max_tokens=20,
        cancel_event=cancel_event,
    ):
        events.append(event)

    boundary.create.assert_not_awaited()
    assert res.cancel_calls == 1, "已取得预留应退款（cancel），不泄漏配额"
    assert any("用户取消" in e for e in events), "应产出用户取消事件而非 LLM 失败"


async def test_llm044_generate_cancel_during_retry_backoff_no_reissue(boundary):
    """LLM-044 P1 复现：非流式 generate 内 retry 退避期间用户取消 → 不发起第二次 create。

    修复前：generate 无执行控制，retry 首次 create 网络超时退避后仍重试发第二次真实
    SDK 请求（LLM-043 检查点只在 generate 门口一次）。修复后：控制贯穿 _budget +
    retry 退避，取消命中即类型化终止（Facade 出口 LLMCancelledError）。
    """
    boundary.retry.config.max_retries = 2
    boundary.retry.config.base_delay = 0.0
    boundary.retry.config.use_jitter = False
    cancel_event = asyncio.Event()

    async def create_once_then_cancel(**kwargs):
        cancel_event.set()  # 首次 create 失败即置取消（模拟取消落在重试窗口内）
        raise httpx.ReadTimeout("downstream timeout")

    boundary.create.side_effect = create_once_then_cancel
    with pytest.raises(LLMCancelledError):
        await boundary.service.generate(
            [{"role": "user", "content": "hi"}],
            max_tokens=20,
            cancel_event=cancel_event,
        )
    assert boundary.create.await_count == 1, "取消后不得发起第二次真实 create"


async def test_llm044_create_inflight_deadline_settles_none(boundary):
    """LLM-044 create_started：create 已在途时 deadline 命中 → settle(None) 保守结算，
    不 cancel() 全额退（请求可能已达 provider，防配额虚增→429）。"""
    async def hang_create(**kwargs):
        await asyncio.Event().wait()  # 模拟 create 挂起（请求在途）

    boundary.create.side_effect = hang_create
    boundary.limiter.reserve.return_value = _CancelTrackingReservation()
    deadline = time.monotonic() + 0.05
    with pytest.raises(LLMDeadlineExceededError):
        await boundary.service.generate(
            [{"role": "user", "content": "hi"}],
            max_tokens=20,
            deadline=deadline,
        )
    res = boundary.limiter.reserve.return_value
    assert res.settle_calls == 1 and res.cancel_calls == 0, \
        "在途 create 终止应 settle(None) 而非 cancel 全额退"


async def test_llm044_cancel_interrupts_reserve_queue_without_sdk_call(boundary):
    """业务取消应立即打断 reserve 排队，且不得创建 SDK 请求。"""
    reserve_started = asyncio.Event()
    cancel_event = asyncio.Event()

    async def hang_reserve(*, estimated_tokens):
        reserve_started.set()
        await asyncio.Event().wait()

    boundary.limiter.reserve.side_effect = hang_reserve
    task = asyncio.create_task(
        boundary.service.generate(
            [{"role": "user", "content": "hi"}],
            max_tokens=20,
            cancel_event=cancel_event,
        )
    )
    await asyncio.wait_for(reserve_started.wait(), timeout=1)
    cancel_event.set()

    with pytest.raises(LLMCancelledError):
        await task
    boundary.create.assert_not_awaited()


async def test_llm044_outer_cancel_after_create_started_settles_once(boundary):
    """外层硬取消与业务 deadline 竞态时，在途请求必须且只能保守结算一次。"""
    create_started = asyncio.Event()
    reservation = _CancelTrackingReservation()
    boundary.limiter.reserve.return_value = reservation

    async def hang_create(**kwargs):
        create_started.set()
        await asyncio.Event().wait()

    boundary.create.side_effect = hang_create
    deadline = time.monotonic() + 60
    task = asyncio.create_task(
        boundary.service.generate(
            [{"role": "user", "content": "hi"}],
            max_tokens=20,
            deadline=deadline,
        )
    )
    await asyncio.wait_for(create_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert reservation.settle_calls == 1
    assert reservation.cancel_calls == 0


# =====================================================================
# LLM-045 迟回值：abort 判赢后 factory 吞取消以值收尾 → 值不被丢弃，
# 由调用方既有 abort 复查接管资源并完成业务收尾
# =====================================================================


async def test_llm045_reserve_swallows_cancel_still_refunds_reservation(
    boundary, monkeypatch
):
    """reserve 跨层：reserve 吞取消迟回已取得的 Reservation → helper 返回 → 第④步复查
    cancel 全额退，SDK create 不被调用（不泄漏配额）。

    修复前：helper 在 ③ 抛类型化信号，迟回 Reservation 无人 cancel → cancel_calls==0。
    """
    cancel_event = asyncio.Event()
    reserve_started = asyncio.Event()
    reservation = _CancelTrackingReservation()

    class _SwallowCancelReserve:
        async def reserve(self, estimated_tokens=0, retry_after=None):
            reserve_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # 吞取消：迟回已取得的预留
            return reservation

        async def reserve_adaptive(self, prompt_tokens=0, max_tokens=0, retry_after=None):
            return reservation

    monkeypatch.setattr(
        ReservationLimiterManager, "get", lambda key: _SwallowCancelReserve()
    )

    task = asyncio.create_task(
        boundary.service.generate(
            [{"role": "user", "content": "hi"}],
            max_tokens=20,
            cancel_event=cancel_event,
        )
    )
    await asyncio.wait_for(reserve_started.wait(), timeout=1)
    cancel_event.set()
    with pytest.raises(LLMCancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert reservation.cancel_calls == 1, "迟回 Reservation 应在第④步复查被全额退款"
    assert reservation.settle_calls == 0
    boundary.create.assert_not_awaited()


async def test_llm045_stream_create_swallows_cancel_late_stream_closed_and_settled(
    boundary, monkeypatch
):
    """流式跨层验收：create 吞取消迟回已打开的 AsyncStream → helper 返回迟回值 →
    整流器接管并立即在已置位 cancel 下关流 + 结算一次；不发起后续 SDK create。

    修复前：helper abort 分支丢弃迟回流（无人 close）+ ⑤ settle(None) → close_calls==0。
    """
    monkeypatch.setattr(LLMService, "_stream_max_retries", 0)  # 显式关闭整流重试来源
    cancel_event = asyncio.Event()
    reservation = _CancelTrackingReservation()
    boundary.limiter.reserve.return_value = reservation

    create_started = asyncio.Event()
    create_calls = 0
    returned_streams = []

    class _LateStream:
        """首 anext 挂起（等四方竞争 abort/close 胜出），close 计数——模拟已打开 SDK 流。"""

        def __init__(self):
            self.close_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()  # 挂起：让 abort 分支胜出（不产出 chunk）

        async def close(self):
            self.close_calls += 1

    async def swallow_cancel_create(**kwargs):
        nonlocal create_calls
        create_calls += 1
        create_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # 吞取消：迟回已取得的流（LLM-045）
        stream = _LateStream()
        returned_streams.append(stream)
        return stream

    monkeypatch.setattr(boundary.client.chat.completions, "create", swallow_cancel_create)

    events = []

    async def collect():
        # 完整消费异步生成器，才能走到整流器的关流 + 结算路径
        async for event in boundary.service.async_generate(
            [{"role": "user", "content": "hi"}],
            model_key="fast",
            max_tokens=20,
            cancel_event=cancel_event,
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await asyncio.wait_for(create_started.wait(), timeout=1)
    cancel_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert create_calls == 1, "取消后不得发起后续 SDK create（整流/续接/fallback 均未发生）"
    assert any("用户取消" in e for e in events), (
        "应按公开契约产出用户取消事件（不假设必然上抛 LLMCancelledError）"
    )
    assert returned_streams and returned_streams[-1].close_calls == 1, (
        "迟回流应被整流器关闭，不能丢弃泄漏 HTTP 连接"
    )
    assert reservation.settle_calls == 1, "res 应由整流器接管结算一次"
    assert reservation.cancel_calls == 0, "请求已发出：不 cancel 全额退"


@pytest.mark.parametrize("abort_kind", ["cancel", "deadline"], ids=["cancel", "deadline"])
async def test_llm045_nonstream_create_swallows_abort_settles_actual_then_aborts_usage_kept(
    boundary, monkeypatch, abort_kind
):
    """非流式 create 吞终止迟回完整 response（LLM-045 迟回 + LLM-047 #5 参数化
    cancel/deadline）：parse + settle(actual usage) → 检查点 3 仍按粘滞 abort 抛
    shared 终止，usage 跨 Facade 不丢（迟回值不作为业务成功返回，费用已按 usage 结算）。"""
    reservation = _CancelTrackingReservation()
    boundary.limiter.reserve.return_value = reservation
    create_started = asyncio.Event()
    create_calls = 0

    usage_obj = SimpleNamespace(
        model_dump=lambda: {
            "prompt_tokens": 3,
            "completion_tokens": 5,
            "total_tokens": 8,
        }
    )
    msg = SimpleNamespace(content="迟回内容", tool_calls=[], reasoning_content=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    response = SimpleNamespace(choices=[choice], usage=usage_obj)

    async def swallow_abort_create(**kwargs):
        nonlocal create_calls
        create_calls += 1
        create_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # 吞终止：迟回完整 response
        return response

    monkeypatch.setattr(boundary.client.chat.completions, "create", swallow_abort_create)

    if abort_kind == "cancel":
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            boundary.service.generate(
                [{"role": "user", "content": "hi"}],
                max_tokens=20,
                cancel_event=cancel_event,
            )
        )
        await asyncio.wait_for(create_started.wait(), timeout=1)
        cancel_event.set()
        with pytest.raises(LLMCancelledError) as exc_info:
            await asyncio.wait_for(task, timeout=2)
    else:
        deadline = time.monotonic() + 0.05
        with pytest.raises(LLMDeadlineExceededError) as exc_info:
            await boundary.service.generate(
                [{"role": "user", "content": "hi"}],
                max_tokens=20,
                deadline=deadline,
            )

    assert exc_info.value.usage == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }, "终止异常应携带迟回 response 结算后的 usage（LLM-047 跨 Facade 成本不丢）"
    assert create_calls == 1
    assert reservation.settle_calls == 1, "迟回 response 已按 usage settle"
    assert reservation.last_actual == 8, "应 settle(actual usage) 而非旧路径 settle(None)"
    assert reservation.cancel_calls == 0


async def test_llm047_stream_rectify_deadline_usage_survives_facade_translation(
    boundary, monkeypatch
):
    """流式整流 deadline 跨层（LLM-047 #4）：首流 usage chunk 后中断（可整流）→ 整流
    退避中/睡满复查 deadline 命中 → rectifier 重建携 usage 的私有信号 → async_generate
    Facade translate_abort 后 LLMDeadlineExceededError.usage 不丢；且不再发起第二次 create。

    修复前整流退避睡满复查裸抛：deadline 命中丢已读 usage（成本不进总账）；本测试对
    退避「被中断」与「睡满复查」两命中时点不敏感（两条出口现均携带 usage）。
    """
    monkeypatch.setattr(LLMService, "_stream_max_retries", 1)
    # 固定整流退避（1s、无抖动）确保 deadline（0.05s）在整流退避段命中（L553/L562），
    # 而非因随机抖动 delay 过短落入下一整流 attempt 的 create 段（该段 usage 已被
    # _reset_dead_meta 清空，裸抛为预期语义）。
    monkeypatch.setattr(StreamingRectifier, "_base_delay", 1.0)
    monkeypatch.setattr(StreamingRectifier, "_max_delay", 30.0)
    monkeypatch.setattr(StreamingRectifier, "_use_jitter", False)
    reservation = _CancelTrackingReservation()
    boundary.limiter.reserve.return_value = reservation

    class _DeadlineStream:
        """usage-only chunk 后中断的流：usage 不算首 token → 可整流。"""

        def __init__(self):
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i == 0:
                self._i += 1
                return SimpleNamespace(
                    choices=[],
                    usage=SimpleNamespace(
                        model_dump=lambda: {
                            "prompt_tokens": 5,
                            "completion_tokens": 3,
                            "total_tokens": 8,
                        }
                    ),
                )
            raise httpx.ReadTimeout("stream reset after usage chunk")

        async def close(self):
            pass

    boundary.create.return_value = _DeadlineStream()
    # deadline 需 > attempt0 整链前置（预算 tiktoken 估算 / reserve / create / 读首流）
    # 耗时（缓冲 0.5s），但 < 整流退避 1.0s —— 确保命中整流退避段（L553/L562）而非
    # attempt 入口（该点无已读 usage，裸抛为预期语义）。
    deadline = time.monotonic() + 0.5
    with pytest.raises(LLMDeadlineExceededError) as exc_info:
        async for _ in boundary.service.async_generate(
            [{"role": "user", "content": "hi"}],
            model_key="fast",
            max_tokens=20,
            deadline=deadline,
        ):
            pass

    assert exc_info.value.usage == {
        "prompt_tokens": 5,
        "completion_tokens": 3,
        "total_tokens": 8,
    }, "整流流已读 usage 跨 Facade translate 不丢（LLM-047）"
    assert boundary.create.await_count == 1, "deadline 命中后不得整流重试第二次 create"
    assert reservation.settle_calls == 1 and reservation.cancel_calls == 0, (
        "请求已发出（create 成功）→ 保守 settle，不 cancel"
    )
    assert reservation.last_actual == 8, "整流中断收尾按已读 usage settle"


async def test_llm_call_log_failure_does_not_override_completed_stream(
    boundary, monkeypatch
):
    """续接 EOF 后日志失败不得覆盖已完成流，也不得触发额外续接。"""

    async def _raise_finish_timeout(*args, **kwargs):
        raise TimeoutError("日志收尾超时")

    monkeypatch.setattr(LLMService, "_stream_max_retries", 0)
    monkeypatch.setattr(LLMService, "_continuation_max_retries", 2)
    monkeypatch.setattr(StreamingRectifier, "_base_delay", 0.0)
    monkeypatch.setattr(StreamingRectifier, "_use_jitter", False)
    monkeypatch.setattr(
        "app.platform.observability.logger.log_event_async", _raise_finish_timeout
    )

    first_reservation = _CancelTrackingReservation()
    continuation_reservation = _CancelTrackingReservation()
    boundary.limiter.reserve.side_effect = [first_reservation, continuation_reservation]
    boundary.create.side_effect = [
        _ScriptedBoundaryStream(
            [_boundary_content_chunk("部分")],
            error=httpx.ReadTimeout("stream reset"),
        ),
        _ScriptedBoundaryStream(
            [_boundary_content_chunk("续写"), _boundary_usage_chunk(8, 3)]
        ),
    ]

    strategy = ReActStrategy(llm=boundary.service, tools=None)
    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        max_iterations=1,
        temperature=0.2,
        max_tokens=20,
        max_execution_time=5.0,
    ):
        pass

    assert boundary.create.await_count == 2, "收尾异常不得触发第三次续接请求"
    assert strategy.outcome is not None
    assert strategy.outcome.error != "Agent 运行异常: TimeoutError"
    assert strategy.outcome.content == "部分续写"
    assert strategy.outcome.usage == {
        "prompt_tokens": 8,
        "completion_tokens": 3,
        "total_tokens": 11,
    }


async def test_continuation_context_overflow_keeps_current_result_and_usage(
    boundary, monkeypatch
):
    """续接前缀撑爆窗口时保留首段结果与已获 usage，并停止所有后续请求。"""

    monkeypatch.setattr(LLMService, "_stream_max_retries", 0)
    monkeypatch.setattr(LLMService, "_continuation_max_retries", 2)
    monkeypatch.setattr(StreamingRectifier, "_base_delay", 0.0)
    monkeypatch.setattr(StreamingRectifier, "_use_jitter", False)
    RequestBudgetManager.register_config({
        "main": RequestBudgetConfig(160, 16),
        "fast": RequestBudgetConfig(1000, 16),
        "fallback": RequestBudgetConfig(300, 16),
    })

    reservation = _CancelTrackingReservation()
    boundary.limiter.reserve.return_value = reservation
    partial = "已有证据" * 80
    boundary.create.return_value = _ScriptedBoundaryStream(
        [_boundary_content_chunk(partial), _boundary_usage_chunk(9, 4)],
        error=httpx.ReadTimeout("stream reset"),
    )

    strategy = ReActStrategy(llm=boundary.service, tools=None)
    events = []
    async for event in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        max_iterations=1,
        temperature=0.2,
        max_tokens=20,
        max_execution_time=5.0,
    ):
        events.append(event)

    assert boundary.create.await_count == 1, "超限续接不得创建新 SDK 请求"
    assert boundary.limiter.reserve.await_count == 1, "预算拒绝必须发生在续接 reserve 前"
    assert reservation.settle_calls == 1 and reservation.last_actual == 13
    assert strategy.outcome is not None
    assert strategy.outcome.content == partial
    assert strategy.outcome.usage == {
        "prompt_tokens": 9,
        "completion_tokens": 4,
        "total_tokens": 13,
    }
    assert "上下文超限" in (strategy.outcome.error or "")
    assert sum('"type": "done"' in event for event in events) == 1


async def test_react_deadline_completes_real_stream_cleanup_before_hard_timeout(
    boundary, monkeypatch
):
    """内部 deadline 后真实 _drain→close→settle→log 在硬取消前完成。"""
    import app.domain.reasoning.react as react_module

    monkeypatch.setattr(react_module, "_EXECUTION_CLEANUP_GRACE_RATIO", 0.5)
    monkeypatch.setattr(react_module, "_MAX_EXECUTION_CLEANUP_GRACE", 0.2)
    monkeypatch.setattr(LLMService, "_stream_max_retries", 0)
    monkeypatch.setattr(LLMService, "_continuation_max_retries", 0)
    monkeypatch.setattr(LLMService, "_fallback_model_id", "")

    order = []
    stream = _HangingBoundaryStream(order, close_delay=0.02)
    reservation = _OrderedReservation(order, settle_delay=0.02)
    boundary.create.return_value = stream
    boundary.limiter.reserve.return_value = reservation

    async def _record_failure(context, *, error, attempt_start):
        order.append("log")

    monkeypatch.setattr(StreamingRectifier, "_log_failure", _record_failure)
    strategy = ReActStrategy(llm=boundary.service, tools=None)
    started = time.monotonic()

    async def _collect():
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            max_iterations=1,
            temperature=0.2,
            max_tokens=20,
            max_execution_time=0.5,
        ):
            pass

    await asyncio.wait_for(_collect(), timeout=1.5)
    elapsed = time.monotonic() - started

    assert order == ["close_started", "close", "settle", "log"]
    assert stream.close_calls == 1
    assert reservation.settle_calls == 1 and reservation.cancel_calls == 0
    assert boundary.create.await_count == 1
    assert boundary.limiter.reserve.await_count == 1
    assert elapsed < 0.5
    assert strategy.outcome is not None
    assert "超时" in (strategy.outcome.error or "")


async def test_react_hard_timeout_cancels_slow_close_and_finally_settles(
    boundary, monkeypatch
):
    """close 超过 grace 时硬墙发出取消，rectifier finally 仍保守结算 Reservation。"""
    import app.domain.reasoning.react as react_module

    monkeypatch.setattr(react_module, "_EXECUTION_CLEANUP_GRACE_RATIO", 0.5)
    monkeypatch.setattr(react_module, "_MAX_EXECUTION_CLEANUP_GRACE", 0.2)
    monkeypatch.setattr(LLMService, "_stream_max_retries", 0)
    monkeypatch.setattr(LLMService, "_continuation_max_retries", 0)
    monkeypatch.setattr(LLMService, "_fallback_model_id", "")

    order = []
    stream = _HangingBoundaryStream(order, hang_on_close=True)
    reservation = _OrderedReservation(order)
    boundary.create.return_value = stream
    boundary.limiter.reserve.return_value = reservation
    strategy = ReActStrategy(llm=boundary.service, tools=None)
    started = time.monotonic()

    async def _collect():
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            max_iterations=1,
            temperature=0.2,
            max_tokens=20,
            max_execution_time=0.5,
        ):
            pass

    await asyncio.wait_for(_collect(), timeout=1.5)
    elapsed = time.monotonic() - started

    assert order == ["close_started", "close_cancelled", "settle"]
    assert stream.close_calls == 1
    assert reservation.settle_calls == 1
    assert reservation.last_actual is None
    assert reservation.cancel_calls == 0
    assert boundary.create.await_count == 1
    assert elapsed < 0.8
    assert strategy.outcome is not None
    assert "超时" in (strategy.outcome.error or "")
