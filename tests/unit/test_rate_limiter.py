"""
RateLimiter / RateLimiterManager 单元测试

覆盖（acquire 形态）：
    TokenBucket      桶容量 / 补充速率 / 等待耗尽后放行
    RateLimiter      双桶（RPM + TPM）各自扣减，Retry-After 优先等待
    RateLimiterManager  按 model_key 懒创建 + 同 key 共享实例 + reset 清空

reserve/settle 形态测试见 test_reservation_limiter.py。

不依赖真实 API：直接用内置 TimeoutError 或短等待断言，不走网络。
"""

import asyncio
import time

import pytest

from app.config import settings
from app.services.llm.rate_limiter import (
    RateLimiter,
    RateLimiterManager,
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
async def test_manager_builds_limiter_from_settings(monkeypatch):
    """manager 按 settings 的 RPM/TPM 建桶。"""
    monkeypatch.setattr(settings, "llm_main_rpm", 5)
    monkeypatch.setattr(settings, "llm_main_tpm", 1_000_000)
    RateLimiterManager.reset()
    limiter = RateLimiterManager.get("main")
    # RPM 桶只有 5 个 token，第 6 次 acquire 需等待
    for _ in range(5):
        await limiter.acquire(estimated_tokens=1)
    start = time.monotonic()
    await limiter.acquire(estimated_tokens=1)
    assert time.monotonic() - start >= 0.5, "RPM 桶应被扣减到需要等待"


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


def test_manager_unknown_key_raises():
    """未知 model_key 抛 ValueError。"""
    RateLimiterManager.reset()
    with pytest.raises(ValueError):
        RateLimiterManager.get("unknown_key")
