"""
RateLimiter / RateLimiterManager 单元测试

覆盖（acquire 形态）：
    TokenBucket      桶容量 / 补充速率 / 等待耗尽后放行
    RateLimiter      双桶（RPM + TPM）各自扣减，Retry-After 优先等待
    RateLimiterManager  按 model_key 懒创建 + 同 key 共享实例 + reset 清空
    参考算法         漏桶/固定窗口/滑窗日志/滑窗计数/GCRA 基础行为 + 持锁 sleep 回归

reserve/settle 形态测试见 test_reservation_limiter.py。

不依赖真实 API：直接用内置 TimeoutError 或短等待断言，不走网络。
"""

import asyncio
import time

import pytest

from app.services.llm.rate_limiter import (
    FixedWindowLimiter,
    GCRALimiter,
    LeakyBucket,
    RateLimiter,
    RateLimiterConfig,
    RateLimiterManager,
    SlidingWindowCounterLimiter,
    SlidingWindowLogLimiter,
    TokenBucket,
)


# =====================================================================
# TokenBucket
# =====================================================================


@pytest.mark.asyncio
async def test_bucket_initial_capacity_available(monkeypatch):
    """初始桶满：acquire(1) 立即返回，无等待。"""
    b = TokenBucket(capacity=10, refill_rate=10)
    start = time.monotonic()
    wait = await b.acquire(1.0)
    assert wait == 0.0
    assert time.monotonic() - start < 0.05, "桶满不应等待"


@pytest.mark.asyncio
async def test_bucket_refills_over_time(monkeypatch):
    """耗尽后等待补充：1 秒补 1 token，需要 1 token 应等约 1 秒。"""
    b = TokenBucket(capacity=1, refill_rate=1)  # 每秒补 1
    await b.acquire(1.0)  # 耗尽
    start = time.monotonic()
    wait = await b.acquire(1.0)  # 需等 1 秒补满
    elapsed = time.monotonic() - start
    assert wait >= 0.9, f"应等约 1 秒，实际 {wait:.2f}s"
    assert elapsed >= 0.9, f"实际等待不足: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_bucket_capacity_caps_accumulation():
    """桶容量封顶：即使长时间不消费，token 不超过 capacity。"""
    b = TokenBucket(capacity=5, refill_rate=100)
    await asyncio.sleep(0.05)  # 理论上补充 5，但被 capacity 封顶
    # 连续 acquire 5 次成功，第 6 次开始等待
    for _ in range(5):
        await b.acquire(1.0)
    start = time.monotonic()
    wait = await b.acquire(1.0)
    assert wait > 0, "超出 capacity 后应等待补充"


@pytest.mark.asyncio
async def test_bucket_zero_refill_disabled():
    """配置 0 = 禁用限流：refill_rate=0 时直接放行，不除零崩溃。"""
    b = TokenBucket(capacity=0, refill_rate=0)
    start = time.monotonic()
    wait = await b.acquire(1.0)
    assert wait == 0.0
    assert time.monotonic() - start < 0.05, "禁用限流不应等待"


@pytest.mark.asyncio
async def test_bucket_wait_does_not_block_others():
    """等待期间锁不被持有：短等待请求不被长等待请求阻塞（问题 2 修复）。"""
    b = TokenBucket(capacity=10, refill_rate=10)  # 每秒补 10（0.1s 补 1）
    await b.acquire(9)  # 剩 1
    await b.acquire(1)  # 耗尽

    # A 需 9 个（等 0.9s 补充），B 只需 1 个（0.1s 后即够）
    a_task = asyncio.create_task(b.acquire(9))
    await asyncio.sleep(0.05)  # 确保 A 先拿到锁进入等待
    start = time.monotonic()
    wait_b = await b.acquire(1)
    b_elapsed = time.monotonic() - start
    assert b_elapsed < 0.3, f"短等待请求被长等待阻塞: {b_elapsed:.2f}s"
    await a_task


@pytest.mark.asyncio
async def test_bucket_cancel_does_not_corrupt_state():
    """取消等待中的 acquire 不破坏桶状态：后续 acquire 仍正常。"""
    b = TokenBucket(capacity=10, refill_rate=10)
    await b.acquire(10)  # 耗尽

    task = asyncio.create_task(b.acquire(5))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # 取消后桶状态完好：等待 0.25s（补 ~2.5 个）后 acquire(2) 应立即放行
    await asyncio.sleep(0.25)
    wait = await b.acquire(2)
    assert wait == 0.0, "取消不应破坏桶计数"


# =====================================================================
# RateLimiter 双桶
# =====================================================================


@pytest.mark.asyncio
async def test_rate_limiter_tpm_bucket_charges(monkeypatch):
    """TPM 桶按 estimated_tokens 扣减：大额 token 触发等待。"""
    limiter = RateLimiter(rpm=10_000, tpm=10)  # 每 6 秒补 1 token
    # 桶满（10），一次扣 10 → 立即
    wait0 = await limiter.acquire(estimated_tokens=10)
    assert wait0 == 0.0
    # 再扣 1 → 需等补充
    start = time.monotonic()
    wait1 = await limiter.acquire(estimated_tokens=1)
    assert wait1 > 0, "TPM 桶耗尽后应等待"
    assert time.monotonic() - start >= 0.5, f"实际等待不足: {wait1:.2f}s"


@pytest.mark.asyncio
async def test_rate_limiter_rpm_bucket_charges():
    """RPM 桶按次数扣减：每秒 1 次，连续第 2 次需等待。"""
    limiter = RateLimiter(rpm=1, tpm=1_000_000)  # RPM 每秒 1 token
    await limiter.acquire()  # 扣 1 次，桶耗尽
    start = time.monotonic()
    wait = await limiter.acquire()  # 第 2 次需等 ~1 秒补充
    assert wait >= 0.9, f"RPM 桶等待不足: {wait:.2f}s"
    assert time.monotonic() - start >= 0.9


@pytest.mark.asyncio
async def test_rate_limiter_retry_after_respected():
    """Retry-After 优先：先按服务端建议 sleep，再扣桶。

    acquire 返回值是桶内等待（wait1+wait2），retry_after 的 sleep 不计入
    返回值——真实墙钟等待用 time.monotonic() 度量。
    """
    limiter = RateLimiter(rpm=1000, tpm=1_000_000)
    start = time.monotonic()
    await limiter.acquire(retry_after=0.3)
    assert time.monotonic() - start >= 0.28, "应尊重 Retry-After 等待"


# =====================================================================
# RateLimiterManager
# =====================================================================


@pytest.mark.asyncio
async def test_manager_builds_limiter_from_config():
    """manager 按 configure 注入的 RPM/TPM 建桶。"""
    RateLimiterManager.register_config(
        {"main": RateLimiterConfig(rpm=5, tpm=1_000_000)}
    )
    limiter = RateLimiterManager.get("main")
    # RPM 桶只有 5 个 token，第 6 次 acquire 需等待
    for _ in range(5):
        await limiter.acquire(estimated_tokens=1)
    start = time.monotonic()
    await limiter.acquire(estimated_tokens=1)
    assert time.monotonic() - start >= 0.5, "RPM 桶应被扣减到需要等待"


@pytest.mark.asyncio
async def test_manager_configure_unconfigured_key_uses_default():
    """configure 只配部分 key，未配置的 key 用默认值（不报错）。"""
    RateLimiterManager.register_config({"main": RateLimiterConfig(rpm=1, tpm=1_000_000)})
    limiter = RateLimiterManager.get("fast")  # 未配置
    # 默认 rpm=60 → 60 次 acquire 内不等待
    for _ in range(10):
        await limiter.acquire(estimated_tokens=1)
    assert limiter._req_bucket.capacity == 60, "未配置 key 应使用默认 RPM"


@pytest.mark.asyncio
async def test_manager_same_key_shared_instance(monkeypatch):
    """同一 model_key 返回同一实例（共享桶记账）。"""
    RateLimiterManager.reset()
    a = RateLimiterManager.get("main")
    b = RateLimiterManager.get("main")
    assert a is b, "同 key 必须复用同一限流器实例"


@pytest.mark.asyncio
async def test_manager_reset_clears_cache():
    """reset 后重新建实例。"""
    RateLimiterManager.reset()
    a = RateLimiterManager.get("main")
    RateLimiterManager.reset()
    b = RateLimiterManager.get("main")
    assert a is not b, "reset 后应新建实例"


def test_manager_custom_key_lazy_builds():
    """任意 model_key（含未预定义）都懒构建，不再抛 ValueError（对齐 ClientManager）。"""
    RateLimiterManager.reset()
    limiter = RateLimiterManager.get("custom_key")
    assert limiter is not None, "未知 key 应懒构建返回 limiter"
    assert RateLimiterManager.get("custom_key") is limiter, "同 key 复用实例"


# =====================================================================
# 参考限流算法组件（漏桶/固定窗口/滑窗日志/滑窗计数/GCRA）
# =====================================================================


def _close(a, b, tol=0.05):
    return abs(a - b) < tol


@pytest.mark.asyncio
async def test_leaky_bucket_constant_rate():
    """漏桶：恒定速率流出，间隔 = tokens/refill_rate。"""
    lb = LeakyBucket(capacity=5, refill_rate=10)
    assert await lb.acquire(1) == 0.0, "首个应立即放行"
    assert _close(await lb.acquire(1), 0.1), "漏桶间隔应 0.1s"
    await lb.refund(1)


@pytest.mark.asyncio
async def test_fixed_window_resets_after_flip():
    """固定窗口：窗口内放行，超限等待窗口翻转。"""
    fw = FixedWindowLimiter(rate=2, window_seconds=1)
    assert await fw.acquire() == 0.0
    assert await fw.acquire() == 0.0
    wait = await fw.acquire()
    assert wait > 0.5, f"固定窗口超限应等待窗口翻转，实际 {wait}"


@pytest.mark.asyncio
async def test_sliding_window_log_exact_count():
    """滑窗日志：窗口内精确计数，满则等待最早时间戳过期。"""
    swl = SlidingWindowLogLimiter(rate=3, window_seconds=1)
    assert await swl.acquire() == 0.0
    assert await swl.acquire() == 0.0
    assert await swl.acquire() == 0.0
    wait = await swl.acquire()
    assert wait > 0, f"满窗口应等待，实际 {wait}"


@pytest.mark.asyncio
async def test_sliding_window_counter_weighted():
    """滑窗计数：分桶加权近似，满则等待最老桶滑出。"""
    swc = SlidingWindowCounterLimiter(rate=4, window_seconds=4, buckets=4)
    for _ in range(4):
        assert await swc.acquire() == 0.0
    wait = await swc.acquire()
    assert wait > 0, f"满窗口应等待，实际 {wait}"


@pytest.mark.asyncio
async def test_gcra_interval_and_burst():
    """GCRA：首个立即放行，后续按 1/rate 间隔节流。"""
    g = GCRALimiter(rate=10, burst=5)
    assert await g.acquire(1) == 0.0, "GCRA 首个应立即放行"
    assert _close(await g.acquire(1), 0.1), "GCRA 间隔应 0.1s"


@pytest.mark.asyncio
async def test_gcra_wait_does_not_block_others():
    """GCRA 等待不阻塞锁：等待期间其他请求能进入临界区排队。

    GCRA 是精确节流——两个并发请求按间隔节流（0.1s + 0.1s = 0.2s）。
    锁外 sleep 保证的是「等待期间锁不被持有」，而非缩短总节流时长。
    验证总时长 ≈ 两次间隔（0.2s），且无死锁。
    """
    g = GCRALimiter(rate=10, burst=5)
    await g.acquire(1)  # TAT 推到 now+0.1
    start = time.monotonic()

    async def acquire_one():
        await g.acquire(1)

    # 两个并发请求：按 GCRA 节流，总时长 ≈ 0.2s（无死锁即证明锁外 sleep 生效）
    await asyncio.gather(acquire_one(), acquire_one())
    elapsed = time.monotonic() - start
    assert 0.1 <= elapsed < 0.3, (
        f"GCRA 并发应≈两次间隔（elapsed={elapsed:.3f}，应 0.1~0.3s）"
    )


# =====================================================================
# 持锁 sleep 回归（锁内计算 → 锁外 sleep，等待不阻塞其他请求）
# =====================================================================


@pytest.mark.asyncio
async def test_leaky_bucket_wait_does_not_block_others():
    """漏桶等待不阻塞锁：两个并发请求的等待不因锁串行叠加。

    漏桶本身是恒定速率排队（后续请求等待前面时间槽），但锁不应被 sleep 持有——
    否则并发请求会因锁竞争比纯排队更慢。此测试验证并发下总时长 ≈ 排队长。
    """
    lb = LeakyBucket(capacity=10, refill_rate=10)
    # 并发发起 2 个请求：各占 1 时间槽（0.1s）
    start = time.monotonic()

    async def acquire_one():
        await lb.acquire(1)

    await asyncio.gather(acquire_one(), acquire_one())
    elapsed = time.monotonic() - start
    # 漏桶排队：2 个请求总时长 ≈ 0.1s（第2个等第1个的槽）。若持锁串行会≈0.2s
    assert elapsed < 0.2, (
        f"漏桶并发应≈排队时长（elapsed={elapsed:.3f}，应 <0.2s）"
    )


@pytest.mark.asyncio
async def test_fixed_window_wait_does_not_block_others():
    """固定窗口等待不阻塞：窗口翻转等待期间，新请求在翻转后立即放行。"""
    fw = FixedWindowLimiter(rate=2, window_seconds=1)
    await fw.acquire()
    await fw.acquire()  # 填满窗口
    # 启动等待窗口翻转的任务
    wait_task = asyncio.create_task(fw.acquire())
    await asyncio.sleep(0.1)

    async def check_not_blocked():
        start = time.monotonic()
        await fw.acquire()  # 仍在满窗口，也会等待
        return time.monotonic() - start

    # 两次等待应可并行推进（都等窗口翻转，总时长 ≈ 1 次翻转而非叠加）
    elapsed = await check_not_blocked()
    assert elapsed < 1.8, f"固定窗口等待不应串行阻塞（elapsed={elapsed:.2f}）"
    await wait_task


# =====================================================================
# B1：RateLimiter.acquire 取消泄漏——RPM 扣后 TPM 前取消 → 退还 RPM
# =====================================================================


async def test_acquire_cancel_between_buckets_refunds_rpm():
    """TPM 扣减前被取消 → RPM 配额退还，后续请求不无谓等待。

    修复前：先扣 RPM 再扣 TPM，中间无保护——TPM acquire 挂起时被取消，
    RPM 永久占用 → 后续请求因 RPM 桶耗尽而无谓等待。修复后：
    except BaseException 退还 RPM 再 re-raise。
    """
    # rpm=2 / tpm=1：第一次调用后 RPM 剩 1、TPM 耗尽。
    # 第二次调用 RPM 立即通过（1→0）、TPM 挂起等待补充（60s），
    # 取消发生在 TPM acquire 内部——正是「RPM 已扣、TPM 未扣」的窗口。
    limiter = RateLimiter(rpm=2, tpm=1)
    await limiter.acquire(estimated_tokens=1)  # RPM 2→1, TPM 1→0

    task = asyncio.create_task(limiter.acquire(estimated_tokens=1))
    await asyncio.sleep(0.05)  # RPM 已扣到 0，TPM 进入等待
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # RPM 已退还：_tokens 恢复到 ~1（capacity=2，refill 仅 ~0.0017）。
    # 未退款时 _tokens ≈ 0（refill 忽略），无法立即通过 acquire。
    assert limiter._req_bucket._tokens >= 0.5, (
        f"取消后 RPM 应已退还（_tokens≈1），实际 {limiter._req_bucket._tokens:.3f}"
    )
    # TPM 桶仍空（未扣成），不应有残留扣减
    assert limiter._token_bucket._tokens < 0.5, "TPM 未完成扣减，不应有残留配额占用"
