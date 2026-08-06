"""
RateLimiter — 客户端限流

使用 Token Bucket 算法，支持按模型独立配置。
集成 Retry-After 响应头的处理。

用法（唯一入口是 acquire，限流是排队而非拒绝）：
    limiter = RateLimiter(rpm=60, tpm=100000)
    await limiter.acquire(estimated_tokens=est)   # 配额不足时等待，够了才继续
    result = await client.chat.completions.create(...)

RateLimiterManager 负责按 model_key 提供共享限流器实例
（RPM / TPM 从配置中心读取，实例跨请求复用，同一模型共享同一个桶）。

reserve/settle 形态见 reservation_limiter.py。
"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

from app.config import settings


class TokenBucket:
    """
    Token Bucket 限流器。

    - capacity: 桶容量（最大突发请求数）
    - refill_rate: 每秒补充 Token 数
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        获取 Token，等待直到可用。

        Args:
            tokens: 需要的 Token 数量

        Returns:
            等待时间（秒）
        """
        # 配置 0 = 禁用限流：refill_rate <= 0 时无限速率直接放行，避免除零崩溃
        # （capacity 与 refill_rate 同源，rpm/tpm 配置为 0 时两者均为 0）
        if self.refill_rate <= 0:
            return 0.0

        # 锁内计算 → 锁外 sleep → 循环重检。
        # sleep 在锁外进行：等待期间锁不被持有，其他排队请求可并行计算；
        # 也保证 sleep 期间能响应取消（CancelledError 不会被锁阻塞吞掉）。
        total_wait = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return total_wait

                # 计算需要等待的时间
                wait_time = (tokens - self._tokens) / self.refill_rate

            # 锁外 sleep：期间其他请求可获取锁、扣减 token
            await asyncio.sleep(wait_time)
            total_wait += wait_time
            # 回到循环顶部重新检查——sleep 期间 token 可能被其他请求抢走，
            # 也可能因容量封顶不需要等满 wait_time，重检保证公平且不过等

    def _refill(self) -> None:
        """补充 Token。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now


class RateLimiter:
    """
    客户端限流器。

    同时限制每分钟请求数（RPM）和每分钟 Token 数（TPM）。
    使用双 Token Bucket 实现。
    """

    def __init__(
        self,
        rpm: int = 60,
        tpm: int = 100_000,
    ) -> None:
        self._req_bucket = TokenBucket(capacity=rpm, refill_rate=rpm / 60)
        self._token_bucket = TokenBucket(capacity=tpm, refill_rate=tpm / 60)

    async def acquire(
        self,
        estimated_tokens: int = 0,
        retry_after: float | None = None,
    ) -> float:
        """
        获取许可。

        Args:
            estimated_tokens: 预估的 Token 消耗（RPM 桶固定扣 1，TPM 桶按此值扣）
            retry_after: 服务端返回的 Retry-After 时间（秒）

        Returns:
            桶内等待时间（秒，wait1 + wait2）；不含 retry_after 的 sleep——
            后者是独立的事前等待，调用方如需完整墙钟等待应自行计时。
        """
        if retry_after:
            await asyncio.sleep(retry_after)

        wait1 = await self._req_bucket.acquire(1.0)
        wait2 = await self._token_bucket.acquire(
            max(estimated_tokens, 1.0),
        )
        return wait1 + wait2


# =====================================================================
# 其他限流算法组件（参考实现，不接入调用链）
# =====================================================================
# 以下为 Token Bucket 之外的主流限流算法（漏桶 / 固定窗口 / 滑动窗口日志 /
# 滑动窗口计数 / GCRA），作为组件提供，供对比与按需选用。接口统一：
#   async acquire(tokens=1.0) -> float   等待型（配额不足时 sleep 等待，返回等待秒数）
#   async refund(tokens=1.0) -> None     退还配额（best-effort，受容量封顶）
# 与 TokenBucket 对齐，可互换使用；未接入 llm_service 调用链。

from collections import deque as _deque


class LeakyBucket:
    """
    漏桶（Leaky Bucket）：恒定速率流出，请求入队排队。

    - capacity: 桶容量（最大排队请求数）
    - refill_rate: 每秒流出速率（出队速率，恒定）

    与 Token Bucket 的区别：**输出速率严格恒定**——即使桶空，新请求也按
    恒定速率流出；空闲期积攒的能力无法用于短时高峰（无突发容忍）。
    桶满时拒绝新请求（本实现为等待型，桶满则等待空位而非拒绝）。

    适用：需要严格平滑输出速率的场景（流量整形），如视频流、队列背压。
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._next_ready = 0.0  # 下一次可放行的时刻（单调时钟）
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        if self.refill_rate <= 0:
            return 0.0
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                # 桶满检查：等待中的请求数 × 每请求时间槽 ≥ 容量
                if self._next_ready > now and (
                    (self._next_ready - now) * self.refill_rate >= self.capacity
                ):
                    # 桶满：等待第一个时间槽空出（锁外 sleep）
                    wait_for_slot = (self._next_ready - now) - (
                        self.capacity / self.refill_rate
                    )
                    wait_for_slot = max(wait_for_slot, 0.0)
                else:
                    wait_for_slot = 0.0
                # 恒定速率：每 tokens 个请求占用 tokens/refill_rate 秒时间槽
                wait = max(self._next_ready - now, 0.0)
                # 若无等待，锁内直接扣减（时间槽推进）返回
                if wait == 0.0 and wait_for_slot == 0.0:
                    self._next_ready = (
                        max(now, self._next_ready) + tokens / self.refill_rate
                    )
                    return total_wait

            # 锁外 sleep：桶满等待 + 恒定速率等待，期间锁不被持有
            sleep_for = max(wait_for_slot, wait)
            await asyncio.sleep(sleep_for)
            total_wait += sleep_for
            # 回到循环顶部重检：sleep 期间可能被其他请求抢时间槽，
            # 也可能已无需等待（容量/时间槽推进），重检保证公平且不过等

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：回退时间槽（best-effort，不低于 now）。"""
        async with self._lock:
            self._next_ready = max(
                time.monotonic(), self._next_ready - tokens / self.refill_rate
            )


class FixedWindowLimiter:
    """
    固定窗口（Fixed Window）：按固定时间窗计数，窗口内达到上限即拒绝。

    - rate: 每窗口最大请求数
    - window_seconds: 窗口长度（秒）

    最简单：一个计数器 + 窗口边界判断，内存常数。
    缺点：**窗口边界双倍请求**——窗口末尾用满配额、下一窗口又用满配额，
    两个窗口交界瞬间实际发出两倍请求（跨窗口交界突发无防护）。

    适用：对瞬时峰值不敏感、实现优先的场景。
    """

    def __init__(self, rate: float, window_seconds: float) -> None:
        self.rate = rate
        self.window_seconds = window_seconds
        self._window_start = 0.0
        self._count = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                if now - self._window_start >= self.window_seconds:
                    self._window_start = now
                    self._count = 0.0
                if self._count + tokens <= self.rate:
                    self._count += tokens
                    return total_wait
                # 窗口内已满：计算窗口翻转剩余时间，锁外 sleep
                wait = self.window_seconds - (now - self._window_start)

            # 锁外 sleep：等待窗口翻转，期间锁不被持有
            await asyncio.sleep(wait)
            total_wait += wait
            # 回到循环顶部重检：窗口已翻转则扣减放行

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：递减当前窗口计数（best-effort，不低于 0）。"""
        async with self._lock:
            self._count = max(0.0, self._count - tokens)


class SlidingWindowLogLimiter:
    """
    滑动窗口日志（Sliding Window Log）：记录窗口内每次请求时间戳，精确计数。

    - rate: 窗口内最大请求数
    - window_seconds: 窗口长度（秒）

    最精确：任意时刻窗口内的请求数真实反映，无边界双倍问题。
    缺点：**内存随窗口内请求量增长**（每请求一个时间戳），高吞吐下开销大。

    适用：低 QPS 但要求精确的场景。
    """

    def __init__(self, rate: float, window_seconds: float) -> None:
        self.rate = rate
        self.window_seconds = window_seconds
        self._timestamps: _deque[float] = _deque()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self.window_seconds
                while self._timestamps and self._timestamps[0] < cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) + tokens <= self.rate:
                    # 记录 tokens 个时间戳（近似：为按量支持，重复记录）
                    for _ in range(int(tokens)):
                        self._timestamps.append(now)
                    return total_wait
                # 窗口已满：计算最早时间戳过期剩余时间，锁外 sleep
                if self._timestamps:
                    wait = self._timestamps[0] + self.window_seconds - now
                else:
                    wait = 0.0

            # 锁外 sleep：等待最早时间戳过期，期间锁不被持有
            if wait > 0:
                await asyncio.sleep(wait)
                total_wait += wait
            # 回到循环顶部重检：过期时间戳已剔除则放行

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：移除最早的时间戳（best-effort）。"""
        async with self._lock:
            for _ in range(int(tokens)):
                if self._timestamps:
                    self._timestamps.popleft()


class SlidingWindowCounterLimiter:
    """
    滑动窗口计数（Sliding Window Counter）：固定窗口 + 分桶加权折中。

    - rate: 每窗口最大请求数
    - window_seconds: 窗口长度（秒）
    - buckets: 分桶数（默认 4）

    把窗口切成 N 个小桶，滑动时按剩余比例加权上一桶计数——比固定窗口平滑
    （无边界双倍），比滑动窗口日志省内存（常数桶数）。

    近似公式：current_count + previous_count × (1 - elapsed/total) ≤ rate
    缺点：分桶粒度内仍不精确（边界内的小幅突刺）。

    适用：Redis 分桶实现（INCR + EXPIRE）、生产网关的常见选择。
    """

    def __init__(self, rate: float, window_seconds: float, buckets: int = 4) -> None:
        self.rate = rate
        self.window_seconds = window_seconds
        self.buckets = max(buckets, 1)
        self._bucket_size = window_seconds / self.buckets
        self._counts: _deque[tuple[float, float]] = _deque()  # (bucket_start, count)
        self._lock = asyncio.Lock()

    def _current_bucket(self, now: float) -> tuple[float, float]:
        """返回 (当前桶起始时刻, 当前桶计数)；过期桶清理。"""
        cutoff = now - self.window_seconds
        while self._counts and self._counts[0][0] < cutoff:
            self._counts.popleft()
        current_start = now - (now % self._bucket_size)
        if self._counts and self._counts[-1][0] == current_start:
            return self._counts[-1]
        self._counts.append((current_start, 0.0))
        return self._counts[-1]

    def _window_count(self, now: float) -> float:
        """滑动窗口近似计数：当前桶 + 上一桶 × 剩余比例。"""
        current_start = now - (now % self._bucket_size)
        current = 0.0
        prev = 0.0
        for start, count in self._counts:
            if start == current_start:
                current = count
            else:
                prev += count
        elapsed_in_bucket = now - current_start
        weight = 1.0 - elapsed_in_bucket / self.window_seconds
        return current + prev * weight

    async def acquire(self, tokens: float = 1.0) -> float:
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._current_bucket(now)
                if self._window_count(now) + tokens <= self.rate:
                    self._counts[-1] = (self._counts[-1][0], self._counts[-1][1] + tokens)
                    return total_wait
                # 窗口已满：计算最老的桶滑出窗口剩余时间，锁外 sleep
                if self._counts:
                    oldest = self._counts[0][0]
                    wait = oldest + self.window_seconds - now
                else:
                    wait = 0.0

            # 锁外 sleep：等待最老的桶滑出窗口，期间锁不被持有
            if wait > 0:
                await asyncio.sleep(wait)
                total_wait += wait
            # 回到循环顶部重检：过期桶已清理则放行

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：递减当前桶计数（best-effort，不低于 0）。"""
        async with self._lock:
            now = time.monotonic()
            bucket = self._current_bucket(now)
            self._counts[-1] = (bucket[0], max(0.0, bucket[1] - tokens))


class GCRALimiter:
    """
    GCRA（Generic Cell Rate Algorithm）：以「理论到达时间 TAT」为核心的精确节流。

    - rate: 每秒速率（个/秒）
    - burst: 突发容忍量（可连续放行的最大数）

    只存一个 TAT，内存常数 + 精确节流（无边界双倍）。常被视为 Token Bucket
    的精确等价形式：TAT 相当于"桶满时刻"，burst 相当于"容量"。

    TAT = 上次请求的理论到达时间
    新请求 → TAT' = max(now, TAT) + 1/rate
      若 TAT' - now > burst/rate → 拒绝（超出突发容忍）
      否则 → 接受，TAT = TAT'

    适用：需要精确节流 + 常数内存的场景；很多实现（如 x/time/rate 的
    advance）本质等价于 GCRA。
    """

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = rate
        self.burst = burst
        self._tat = 0.0  # 理论到达时间（单调时钟）
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        if self.rate <= 0:
            return 0.0
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                # 等待时间 = max(0, 前一个 TAT - now)；首个请求 TAT=0 → 立即放行
                wait = max(self._tat - now, 0.0)
                if wait == 0.0:
                    # 无等待：锁内直接更新 TAT 并返回
                    self._tat = max(now, self._tat) + tokens / self.rate
                    return total_wait

            # 锁外 sleep：等待 TAT 到达，期间锁不被持有
            await asyncio.sleep(wait)
            total_wait += wait
            # 回到循环顶部重检：TAT 已到则更新并放行（sleep 期间可能被其他请求推进 TAT）

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：回退 TAT（best-effort，不低于 now）。"""
        async with self._lock:
            self._tat = max(time.monotonic(), self._tat - tokens / self.rate)


# =====================================================================
# 限流器管理
# =====================================================================


# model_key → 读取 settings 中的 RPM / TPM 配置字段名
_RATE_LIMIT_FIELDS: dict[str, tuple[str, str]] = {
    "main": ("llm_main_rpm", "llm_main_tpm"),
    "reasoning": ("llm_reasoning_rpm", "llm_reasoning_tpm"),
    "fast": ("llm_fast_rpm", "llm_fast_tpm"),
}


class RateLimiterManager:
    """
    按 model_key 提供共享限流器实例。

    与 ClientManager 同款缓存模式：同一 model_key 复用同一个
    RateLimiter（双 Token Bucket 跨请求记账，不能每次 new）。
    配置（RPM / TPM）从配置中心懒加载，修改配置后 reset() 重建。
    """

    _instances: ClassVar[dict[str, RateLimiter]] = {}

    @classmethod
    def get(cls, model_key: str = "main") -> RateLimiter:
        """获取指定 key 的限流器（懒创建 + 缓存复用）。

        Args:
            model_key: 模型标识（main / reasoning / fast）

        Returns:
            共享 RateLimiter 实例

        Raises:
            ValueError: model_key 未在限流配置映射中
        """
        if model_key in cls._instances:
            return cls._instances[model_key]

        fields = _RATE_LIMIT_FIELDS.get(model_key)
        if fields is None:
            raise ValueError(f"未知限流 key: {model_key!r}")

        rpm_field, tpm_field = fields
        limiter = RateLimiter(
            rpm=getattr(settings, rpm_field, 0),
            tpm=getattr(settings, tpm_field, 0),
        )
        cls._instances[model_key] = limiter
        return limiter

    @classmethod
    def reset(cls) -> None:
        """清空所有缓存实例（配置变更或测试时调用）。"""
        cls._instances.clear()
