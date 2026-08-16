"""
ReservationLimiter / ReservationLimiterManager 单元测试（reserve/settle 形态）

覆盖：
    TokenBucket（自包含）     桶容量 / 补充速率 / refund 退还 / acquire 等待
    Reservation               settle 退差 / settle(None) 保守 / cancel 全额 / 幂等
    ReservationLimiter        双桶组合：reserve 扣减 / settle 只退 TPM / cancel 退 RPM+TPM
    ReservationLimiterManager  按 model_key 懒创建 + 同 key 共享实例 + reset 清空
    OutputTokenEstimator      冷启动 / 分位数 / 安全系数 / 窗口封顶 / reset
    reserve_adaptive          冷启动回退静态 / 有样本用估算 / clamp / 分池 / settle 喂样本

不依赖真实 API：直接用内置 TimeoutError 或短等待断言，不走网络。
"""

import asyncio
import time

import pytest

from app.integration.llm.reservation_limiter import (
    OutputTokenEstimator,
    Reservation,
    ReservationLimiter,
    ReservationLimiterConfig,
    ReservationLimiterManager,
    TokenBucket,
)


# =====================================================================
# TokenBucket（自包含）refund / acquire
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


@pytest.mark.asyncio
async def test_bucket_acquire_waits_when_empty():
    """桶空后 acquire 等待补充（reserve 底层复用该等待）。"""
    b = TokenBucket(capacity=10, refill_rate=10)
    await b.acquire(10)  # 耗尽
    start = time.monotonic()
    await b.acquire(1)
    assert time.monotonic() - start >= 0.05, "桶空后 acquire 应等待补充"


@pytest.mark.asyncio
async def test_bucket_acquire_oversized_does_not_hang():
    """tokens > capacity：截断到容量立即放行，不无限等待。

    修复前：_refill 将 _tokens 封顶于 capacity，tokens > capacity 时
    _tokens >= tokens 永假，while True 永不退出 → 请求永久挂起。
    修复后：截断到 capacity，桶满立即返回 0.0（wait_for 超时保护兜底）。
    """
    b = TokenBucket(capacity=10, refill_rate=10)
    wait = await asyncio.wait_for(b.acquire(100), timeout=1)
    assert wait == 0.0, "超容量请求应截断到容量并立即放行"
    # 截断后桶被扣 capacity：桶空，后续 acquire(1) 需等待补充
    start = time.monotonic()
    await asyncio.wait_for(b.acquire(1), timeout=1)
    assert time.monotonic() - start >= 0.05, "截断扣减后桶空，应等待补充"


# =====================================================================
# Reservation — 预留对象（settle/cancel 语义）
# =====================================================================


async def _reserve_single(bucket: TokenBucket, reserved: float) -> Reservation:
    """构造单桶预留：acquire 扣减 + 记录条目（模拟 reserve 流程）。"""
    await bucket.acquire(reserved)
    res = Reservation()
    res.add(bucket, reserved)
    return res


@pytest.mark.asyncio
async def test_reservation_settle_refunds_difference():
    """settle(actual) 退 max(0, reserved - actual)：多预的退回来。"""
    b = TokenBucket(capacity=100, refill_rate=100)
    res = await _reserve_single(b, 10)
    await res.settle(4)
    assert res.settled
    # 退还 6，桶回到 96 但被 capacity 封顶为 100 → 立即能 acquire 96
    await b.acquire(96)


@pytest.mark.asyncio
async def test_reservation_settle_none_keeps_reserved():
    """settle(None) 保留全部预留（保守），但标记终态。"""
    b = TokenBucket(capacity=100, refill_rate=100)
    res = await _reserve_single(b, 10)
    await res.settle(None)
    assert res.settled
    # 后续 settle/cancel 为 no-op
    await res.cancel()
    await res.settle(0)


@pytest.mark.asyncio
async def test_reservation_cancel_refunds_all():
    """cancel 全额退还。"""
    b = TokenBucket(capacity=100, refill_rate=100)
    res = await _reserve_single(b, 10)
    await res.cancel()
    assert res.settled
    # 全额退还后桶应满：立即 acquire(100) 无等待
    await b.acquire(100)


@pytest.mark.asyncio
async def test_reservation_idempotent():
    """settle/cancel 幂等：重复调用不重复退款。"""
    b = TokenBucket(capacity=10, refill_rate=10)
    res = await _reserve_single(b, 6)
    await res.settle(2)  # 退 4
    await res.settle(2)  # no-op
    await res.cancel()   # no-op
    # 桶内：初始 10 - 6 + 4 = 8
    await b.acquire(8)


@pytest.mark.asyncio
async def test_reservation_empty_entries_noop():
    """空预留（无条目）settle/cancel 为 no-op 并置终态。"""
    res = Reservation()
    await res.settle(5)
    assert res.settled
    await res.cancel()  # no-op


# =====================================================================
# ReservationLimiter 双桶组合
# =====================================================================


@pytest.mark.asyncio
async def test_reservation_limiter_reserve_deducts_both_buckets():
    """reserve 扣减 RPM 1 + TPM estimated，返回未终态组合 Reservation。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000)
    res = await limiter.reserve(estimated_tokens=50)
    assert not res.settled
    # RPM 剩 999（扣 1），TPM 剩 99950（扣 50）
    await limiter._req_bucket.acquire(999)
    await limiter._token_bucket.acquire(99950)


@pytest.mark.asyncio
async def test_reservation_limiter_settle_tpm_only():
    """组合 Reservation：settle 只退 TPM 差，RPM 不退（请求已发出）。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000)
    res = await limiter.reserve(estimated_tokens=50)
    await res.settle(10)
    # RPM 桶应剩 999（只扣 1 不退），TPM 桶应剩 100000-50+40
    await limiter._req_bucket.acquire(999)
    await limiter._token_bucket.acquire(99990)


@pytest.mark.asyncio
async def test_reservation_limiter_cancel_refunds_both():
    """组合 Reservation：cancel 退 RPM 1 + TPM 全额（请求未发出）。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000)
    res = await limiter.reserve(estimated_tokens=50)
    await res.cancel()
    # 全额退还：RPM 桶满（1000），TPM 桶满（100000）
    await limiter._req_bucket.acquire(1000)
    await limiter._token_bucket.acquire(100000)


@pytest.mark.asyncio
async def test_reservation_limiter_reserve_oversized_clamps():
    """reserve 预估超 TPM 桶容量：截断到容量，立即返回（不无限等待）。

    修复前：reserve(estimated_tokens=200) 对 tpm=100 的桶 acquire 死循环
    （_refill 封顶 capacity，_tokens >= tokens 永假），请求永久挂起。
    修复后：TPM 预留截断到桶容量（100），reserve 立即返回。
    """
    limiter = ReservationLimiter(rpm=1000, tpm=100)
    res = await asyncio.wait_for(limiter.reserve(estimated_tokens=200), timeout=1)
    # 预留条目应记录截断后的容量（100），而非 200——settle 退差基础一致
    assert res._entries[-1][1] == 100, (
        f"超容量预留应截断到桶容量 100，实际 {res._entries[-1][1]}"
    )
    await res.cancel()


@pytest.mark.asyncio
async def test_reservation_limiter_reserve_oversized_settle():
    """超容量截断后 settle 退差正确：reserved=capacity，退 max(0, capacity-actual)。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100)
    res = await asyncio.wait_for(limiter.reserve(estimated_tokens=200), timeout=1)
    await res.settle(30)
    assert res.settled
    # 桶扣 100（截断）→ 退 100-30=70 → 剩 70 可立即 acquire，第 71 需等待
    await limiter._token_bucket.acquire(70)
    start = time.monotonic()
    await asyncio.wait_for(limiter._token_bucket.acquire(1), timeout=1)
    assert time.monotonic() - start >= 0.05, "退款后剩余 70，第 71 个应等待"


# =====================================================================
# ReservationLimiterManager
# =====================================================================


@pytest.mark.asyncio
async def test_manager_builds_limiter_from_config():
    """manager 按 configure 注入的 RPM/TPM 建桶。"""
    ReservationLimiterManager.register_config(
        {"main": ReservationLimiterConfig(rpm=5, tpm=1_000_000)}
    )
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


def test_manager_custom_key_lazy_builds():
    """任意 model_key（含未预定义）都懒构建，不再抛 ValueError（对齐 ClientManager）。"""
    ReservationLimiterManager.reset()
    limiter = ReservationLimiterManager.get("custom_key")
    assert limiter is not None, "未知 key 应懒构建返回 limiter"
    assert ReservationLimiterManager.get("custom_key") is limiter, "同 key 复用实例"


# =====================================================================
# OutputTokenEstimator（自适应预留估算器）
# =====================================================================


def test_estimator_cold_start_returns_zero():
    """冷启动（样本 < min_samples）返回 0，调用方回退静态上限。"""
    est = OutputTokenEstimator(min_samples=3)
    assert est.estimate() == 0
    est.record(10)
    est.record(20)
    assert est.estimate() == 0, "样本不足 min_samples 应返回 0"
    est.record(30)  # 达到 min_samples
    assert est.estimate() > 0


def test_estimator_quantile_value():
    """分位数确定性：range(100) 的 p50 = 49。"""
    est = OutputTokenEstimator(quantile=0.5, safety_margin=1.0, min_samples=1)
    for i in range(100):
        est.record(i)
    assert est.estimate() == 49


def test_estimator_negative_quantile_clamps_to_min():
    """负 quantile（配置异常）clamp 到 0——取最小值而非负索引取倒数元素。

    修复前：int(self.quantile * (len(ordered) - 1)) 为负索引，Python 取倒数元素，
    语义错反且难发现（quantile=-1.0 → 取倒数第 2 个而非最小值）。
    """
    est = OutputTokenEstimator(quantile=-1.0, safety_margin=1.0, min_samples=1)
    for i in range(100):
        est.record(i)
    assert est.estimate() == 0, "负 quantile 应 clamp 到 0（取最小值）"


def test_estimator_quantile_over_one_clamps_to_max():
    """quantile > 1（配置异常）clamp 到 1——取最大值而非超界索引。"""
    est = OutputTokenEstimator(quantile=5.0, safety_margin=1.0, min_samples=1)
    for i in range(100):
        est.record(i)
    assert est.estimate() == 99, "quantile>1 应 clamp 到 1（取最大值）"


def test_estimator_safety_margin_applied():
    """安全系数应用：p95 × 2.0。"""
    est = OutputTokenEstimator(quantile=0.95, safety_margin=2.0, min_samples=1)
    for i in range(100):
        est.record(i)
    # p95 of 0..99 = 94；ceil(94 × 2.0) = 188
    assert est.estimate() == 188


def test_estimator_window_caps_samples():
    """滚动窗口封顶：record 超过 window 条只保留最新 window 条。"""
    est = OutputTokenEstimator(min_samples=1, window=256)
    for i in range(300):
        est.record(i)
    assert len(est._samples) == 256


def test_estimator_reset_clears():
    """reset 清空样本，回到冷启动。"""
    est = OutputTokenEstimator(min_samples=1)
    est.record(100)
    assert est.estimate() > 0
    est.reset()
    assert est.estimate() == 0


# =====================================================================
# reserve_adaptive（自适应预留）
# =====================================================================


@pytest.mark.asyncio
async def test_adaptive_reserve_cold_start_falls_back_to_static():
    """冷启动：无样本时预留 = prompt + max_tokens（静态上限）。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000, min_samples=3)
    res = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
    assert res._entries[-1][1] == 100 + 4096, (
        f"冷启动应回退静态上限，实际 {res._entries[-1][1]}"
    )
    await res.cancel()


@pytest.mark.asyncio
async def test_adaptive_reserve_uses_estimate_after_samples():
    """有样本后：预留 = prompt + 高分位估算，远小于静态上限。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000, min_samples=1, safety_margin=1.0)
    # 喂 5 次实际输出 200 → 池内样本 200
    for _ in range(5):
        r = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
        await r.settle(100 + 200)  # total=300 → completion=200
    r2 = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
    reserved = r2._entries[-1][1]
    assert reserved == 100 + 200, f"有样本后预留应≈prompt+200，实际 {reserved}"
    assert reserved < 100 + 4096, "预留应远小于静态上限"
    await r2.cancel()


@pytest.mark.asyncio
async def test_adaptive_reserve_clamps_to_max_tokens():
    """clamp：样本估算超过 max_tokens 时预留封顶到 max_tokens（只减不加）。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000, min_samples=1, safety_margin=1.0)
    for _ in range(5):
        r = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=512)
        await r.settle(100 + 100_000)  # 实际输出超大
    r2 = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=512)
    reserved = r2._entries[-1][1]
    assert reserved == 100 + 512, f"clamp 到 max_tokens，实际 {reserved}"
    await r2.cancel()


@pytest.mark.asyncio
async def test_adaptive_reserve_pools_by_max_tokens():
    """按 max_tokens 分池：4096 池有样本、512 池冷启动 → 512 回退静态。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000, min_samples=1, safety_margin=1.0)
    # 4096 池喂样本
    for _ in range(5):
        r = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
        await r.settle(100 + 200)
    # 512 池冷启动 → 回退静态
    r2 = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=512)
    assert r2._entries[-1][1] == 100 + 512, "512 池冷启动应回退静态"
    # 4096 池有样本 → 用估算
    r3 = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
    assert r3._entries[-1][1] == 100 + 200, "4096 池应用估算"
    await r2.cancel()
    await r3.cancel()


@pytest.mark.asyncio
async def test_adaptive_settle_feeds_estimator():
    """settle(actual) 喂估算器：total - prompt = completion；settle(None) 不喂。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000, min_samples=1, safety_margin=1.0)
    r = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
    await r.settle(300)  # total=300 → completion=200
    est = limiter._estimator_for(4096)
    assert list(est._samples) == [200], f"应记录 completion=200，实际 {list(est._samples)}"
    # settle(None) 不喂样本
    r2 = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
    await r2.settle(None)
    assert len(est._samples) == 1, "settle(None) 不应喂样本"


@pytest.mark.asyncio
async def test_reserve_backward_compat():
    """reserve 向后兼容：固定形态仍扣 estimated（开关关路径回归）。"""
    limiter = ReservationLimiter(rpm=1000, tpm=100_000)
    res = await limiter.reserve(estimated_tokens=50)
    assert res._entries[-1][1] == 50, "固定形态仍按 estimated 扣"
    await res.cancel()


@pytest.mark.asyncio
async def test_manager_reasoning_gets_p99():
    """Manager：configure 注入的 reasoning 用 p99 分位，main 用 p95。"""
    ReservationLimiterManager.register_config(
        {
            "main": ReservationLimiterConfig(quantile=0.95),
            "reasoning": ReservationLimiterConfig(quantile=0.99),
        }
    )
    main_limiter = ReservationLimiterManager.get("main")
    reasoning_limiter = ReservationLimiterManager.get("reasoning")
    assert main_limiter._quantile == 0.95
    assert reasoning_limiter._quantile == 0.99


# =====================================================================
# 取消泄漏：settle/cancel 退款中途被取消 → 保持未终态，外层可兜底续退
# =====================================================================


class _CancelOnRefundBucket:
    """在指定次数的 refund 时抛 CancelledError 的桩桶（模拟退款中途被取消）。

    每次 refund 前先检查：若已触发过 cancel 且本次是第 cancel_at 次 refund，
    则 raise CancelledError——用于测试 settle/cancel 循环中途的取消中断。
    """

    def __init__(self, capacity: float = 1000.0) -> None:
        self.capacity = capacity
        self._tokens = capacity
        self.refunds = 0
        self._cancel_at: int | None = None

    def cancel_on_refund(self, n: int) -> None:
        """设定第 n 次 refund 时抛 CancelledError。"""
        self._cancel_at = n

    async def acquire(self, tokens: float = 1.0) -> float:
        """返回等待时间：桶满（可全量取）返回 0，否则返回正等待时长。"""
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        return 1.0  # 非零即表示配额不足（测试仅断言"是否立即可取"）

    async def refund(self, tokens: float = 1.0) -> None:
        self.refunds += 1
        if self._cancel_at is not None and self.refunds == self._cancel_at:
            raise asyncio.CancelledError()
        self._tokens = min(self.capacity, self._tokens + tokens)


async def test_settle_cancelled_midway_keeps_unsettled_and_cancel_refunds_all():
    """settle 退款中途被取消 → _settled 保持 False，后续 cancel 可全额续退。

    修复前：settle 循环开始前就置 _settled=True，中途取消后剩余桶退款丢失、
    重入 no-op → 配额永久泄漏。修复后：终态标记移到全部退款完成后，
    取消中断时保持未终态，外层兜底 cancel 可续退全部条目。
    """
    # 按次桶 + 两个按量桶：组合预留 cancel 需退 3 个桶
    rpm = TokenBucket(capacity=100, refill_rate=100)
    tpm1 = _CancelOnRefundBucket()
    tpm2 = _CancelOnRefundBucket()
    res = Reservation()
    res.add(rpm, 1.0)
    res.add(tpm1, 10.0)
    res.add(tpm2, 10.0)

    tpm1.cancel_on_refund(1)  # settle 第一个按量桶退款时取消

    with pytest.raises(asyncio.CancelledError):
        await res.settle(5)

    assert not res.settled, "取消中断时不得标记终态（外层兜底需能续退）"

    # 外层兜底 cancel：继续全额退还全部桶（容量封顶保证已退部分不超发）
    await res.cancel()
    assert res.settled, "cancel 完成后应标记终态"

    # 全部配额已退还：各桶满，可全量 acquire 且无等待
    assert await rpm.acquire(100) == 0.0, "按次桶应已全额退还"
    assert await tpm1.acquire(1000) == 0.0, "按量桶1应已全额退还"
    assert await tpm2.acquire(1000) == 0.0, "按量桶2应已全额退还"


async def test_cancel_cancelled_midway_keeps_unsettled():
    """cancel 退款中途被取消 → _settled 保持 False，可重试 cancel 补齐。"""
    b1 = TokenBucket(capacity=100, refill_rate=100)
    b2 = _CancelOnRefundBucket()
    res = Reservation()
    res.add(b1, 5.0)
    res.add(b2, 5.0)

    b2.cancel_on_refund(1)  # 第一个桶已退，第二个桶退款时取消

    with pytest.raises(asyncio.CancelledError):
        await res.cancel()

    assert not res.settled, "取消中断时不得标记终态"

    # 重试 cancel：补齐剩余退款（已退部分由容量封顶保证不超发）
    await res.cancel()
    assert res.settled

    # 全部配额已退还：两个桶都满，可全量 acquire 且无等待
    assert await b1.acquire(100) == 0.0, "桶1应已全额退还"
    assert await b2.acquire(1000) == 0.0, "桶2应已全额退还"
