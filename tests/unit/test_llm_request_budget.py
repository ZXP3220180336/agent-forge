"""预算闸跨 Facade / 真实重试器 / 整流器 / 结构化输出的边界回归。"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.integration.llm.client import ClientManager
from app.integration.llm.llm_service import LLMService
from app.integration.llm.request_budget import RequestBudgetConfig, RequestBudgetManager
from app.integration.llm.reservation_limiter import ReservationLimiterManager
from app.integration.llm.retry import CircuitState, RetryConfig, RetryHandler, RetryHandlerManager
from app.shared.exceptions import ContextWindowExceededError


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
    return SimpleNamespace(service=LLMService(), retry=retry, create=create, limiter=limiter)


class _CancelTrackingReservation:
    """记录 cancel/settle 的 Reservation 桩（终态语义对齐真实实现）。"""

    def __init__(self) -> None:
        self.settled = False
        self.cancel_calls = 0
        self.settle_calls = 0

    async def settle(self, actual=None):
        self.settle_calls += 1
        self.settled = True

    async def cancel(self):
        self.cancel_calls += 1
        self.settled = True


def _open_circuit(boundary) -> None:
    """置熔断 OPEN 且冷却未过（_last_failure_time 指向未来）。

    仅置 ``_state = OPEN`` 不够：OPEN 超过 recovery_timeout 会自动转 HALF_OPEN
    并放行一个主链路探针（allow_request），并非真走 fallback——原「共享主窗口」
    用例正是因此实际测的是主链路预算闸而非 fallback 路径。
    """
    boundary.retry.circuit_breaker._state = CircuitState.OPEN
    boundary.retry.circuit_breaker._last_failure_time = time.monotonic() + 3600


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
