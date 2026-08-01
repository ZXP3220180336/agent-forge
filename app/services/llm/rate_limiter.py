"""
RateLimiter — 客户端限流

使用 Token Bucket 算法，支持按模型独立配置。
集成 Retry-After 响应头的处理。

用法：
    limiter = RateLimiter(rpm=60, tpm=100000)
    async with limiter:
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
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0

            # 计算需要等待的时间
            needed = tokens - self._tokens
            wait_time = needed / self.refill_rate
            await asyncio.sleep(wait_time)

            self._refill()
            self._tokens -= tokens
            return wait_time

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
            estimated_tokens: 预估的 Token 消耗
            retry_after: 服务端返回的 Retry-After 时间

        Returns:
            总等待时间（秒）
        """
        if retry_after:
            await asyncio.sleep(retry_after)

        wait1 = await self._req_bucket.acquire(1.0)
        wait2 = await self._token_bucket.acquire(
            max(estimated_tokens, 1.0),
        )
        return wait1 + wait2

    async def __aenter__(self) -> RateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


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
