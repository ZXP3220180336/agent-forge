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
