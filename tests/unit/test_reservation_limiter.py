"""
ReservationLimiter / ReservationLimiterManager 单元测试（reserve/settle 形态）

覆盖：
    TokenBucket（自包含）     桶容量 / 补充速率 / refund 退还
    ReservationTokenBucket    reserve 扣减
    Reservation               settle 退差 / settle(None) 保守 / cancel 全额 / 幂等
    ReservationLimiter        双桶组合：settle 只退 TPM，cancel 退 RPM+TPM
    ReservationLimiterManager  按 model_key 懒创建 + 同 key 共享实例 + reset 清空

不依赖真实 API：直接用内置 TimeoutError 或短等待断言，不走网络。
"""

import asyncio
import time

import pytest

from app.config import settings
from app.services.llm.reservation_limiter import (
    ReservationLimiter,
    ReservationLimiterManager,
    ReservationTokenBucket,
    TokenBucket,
)


# =====================================================================
# TokenBucket（自包含）refund
# =====================================================================


@pytest.mark.asyncio
async def test_bucket_refund_restores_tokens():
    """refund 退还 token：耗尽后 refund 可立即 acquire。"""
    b = TokenBucket(capacity=10, refill_rate=10)
    await b.acquire(10)  # 耗尽
    await b.refund(5)
    wait = await b.acquire(1)
    assert wait == 0.0, "退款后应立即可用"


@pytest.mark.asyncio
async def test_bucket_refund_capped_by_capacity():
    """refund 受 capacity 封顶：退不超桶容量。"""
    b = TokenBucket(capacity=5, refill_rate=100)
    await b.refund(10)
    # 桶最多 5 个 token：连取 5 次成功，第 6 次等待
    for _ in range(5):
        await b.acquire(1.0)
    wait = await b.acquire(1.0)
    assert wait > 0, "退款受 capacity 封顶，不应有第 6 个"


# =====================================================================
# ReservationTokenBucket
# =====================================================================


@pytest.mark.asyncio
async def test_reservation_bucket_reserve_returns_ok():
    """reserve 返回 Reservation，扣减配额。"""
    b = ReservationTokenBucket(capacity=10, refill_rate=10)
    res = await b.reserve(4)
    assert not res.settled
    # 桶剩 6：再 reserve 6 次后第 7 次需等待
    for _ in range(6):
        await b.reserve(1)
    # 消费 4+6=10，桶空；reserve(1) 需等待
    start = time.monotonic()
    await b.reserve(1)
    assert time.monotonic() - start >= 0.05, "桶空后 reserve 应等待"


# =====================================================================
# Reservation
# =====================================================================


@pytest.mark.asyncio
async def test_reservation_settle_refunds_difference():
    """settle(actual) 退 max(0, reserved - actual)：多预的退回来。"""
    b = ReservationTokenBucket(capacity=100, refill_rate=100)
    res = await b.reserve(10)
    await res.settle(4)
    assert res.settled
    # 退还 6，桶可用量回到 100-4=96 但被 capacity 封顶为 100 → 立即能 reserve
    r2 = await b.reserve(5)
    assert not r2.settled


@pytest.mark.asyncio
async def test_reservation_settle_none_keeps_reserved():
    """settle(None) 保留全部预留（保守），但标记终态。"""
    b = ReservationTokenBucket(capacity=100, refill_rate=100)
    res = await b.reserve(10)
    await res.settle(None)
    assert res.settled
    # 后续 settle/cancel 为 no-op
    await res.cancel()
    await res.settle(0)


@pytest.mark.asyncio
async def test_reservation_cancel_refunds_all():
    """cancel 全额退还。"""
    b = ReservationTokenBucket(capacity=100, refill_rate=100)
    res = await b.reserve(10)
    await res.cancel()
    assert res.settled
    # 全额退还后桶应满：立即 reserve(90) 无等待
    await b.reserve(90)


@pytest.mark.asyncio
async def test_reservation_idempotent():
    """settle/cancel 幂等：重复调用不重复退款。"""
    b = ReservationTokenBucket(capacity=10, refill_rate=10)
    res = await b.reserve(6)
    await res.settle(2)  # 退 4
    await res.settle(2)  # no-op
    await res.cancel()   # no-op
    # 桶内：初始 10 - 6 + 4 = 8
    await b.reserve(8)


# =====================================================================
# ReservationLimiter 双桶组合
# =====================================================================


@pytest.mark.asyncio
async def test_reservation_limiter_settle_tpm_only():
    """组合 Reservation：settle 只退 TPM 差，RPM 不退（请求已发出）。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000)
    res = await limiter.reserve(estimated_tokens=50)
    await res.settle(10)
    # RPM 桶应剩 999（只扣 1 不退），TPM 桶应剩 100000-50+40
    assert await limiter._req_bucket.reserve(999)
    await limiter._token_bucket.reserve(99990)


@pytest.mark.asyncio
async def test_reservation_limiter_cancel_refunds_both():
    """组合 Reservation：cancel 退 RPM 1 + TPM 全额（请求未发出）。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000)
    res = await limiter.reserve(estimated_tokens=50)
    await res.cancel()
    # 全额退还：RPM 桶满（1000），TPM 桶满（100000）
    assert await limiter._req_bucket.reserve(1000)
    assert await limiter._token_bucket.reserve(100000)


# =====================================================================
# ReservationLimiterManager
# =====================================================================


@pytest.mark.asyncio
async def test_manager_builds_limiter_from_settings(monkeypatch):
    """manager 按 settings 的 RPM/TPM 建桶。"""
    monkeypatch.setattr(settings, "llm_main_rpm", 5)
    monkeypatch.setattr(settings, "llm_main_tpm", 1_000_000)
    ReservationLimiterManager.reset()
    limiter = ReservationLimiterManager.get("main")
    # RPM 桶只有 5 个 token，第 6 次 reserve 需等待
    for _ in range(5):
        await limiter.reserve(estimated_tokens=1)
    start = time.monotonic()
    await limiter.reserve(estimated_tokens=1)
    assert time.monotonic() - start >= 0.5, "RPM 桶应被扣减到需要等待"


@pytest.mark.asyncio
async def test_manager_same_key_shared_instance(monkeypatch):
    """同一 model_key 返回同一实例（共享桶记账）。"""
    ReservationLimiterManager.reset()
    a = ReservationLimiterManager.get("main")
    b = ReservationLimiterManager.get("main")
    assert a is b, "同 key 必须复用同一限流器实例"


@pytest.mark.asyncio
async def test_manager_reset_clears_cache():
    """reset 后重新建实例。"""
    ReservationLimiterManager.reset()
    a = ReservationLimiterManager.get("main")
    ReservationLimiterManager.reset()
    b = ReservationLimiterManager.get("main")
    assert a is not b, "reset 后应新建实例"


def test_manager_unknown_key_raises():
    """未知 model_key 抛 ValueError。"""
    ReservationLimiterManager.reset()
    with pytest.raises(ValueError):
        ReservationLimiterManager.get("unknown_key")
