"""
ReservationLimiter — 客户端限流（reserve/settle 形态）

与 rate_limiter.py（acquire 形态）并存，独立实现、不共用任何代码。
使用 Token Bucket 算法，支持按模型独立配置，集成 Retry-After 处理。

用法（reserve/settle：请求前预留，请求后结算退差）：
    limiter = ReservationLimiter(rpm=60, tpm=100000)
    res = await limiter.reserve(estimated_tokens=est)   # 预留配额（排队而非拒绝）
    result = await client.chat.completions.create(...)
    await res.settle(actual)     # 实际消耗 actual，退还 max(0, est - actual)
    #   或 await res.cancel()    # 请求未发出，全额退还

结算退差：请求完成后把未用完的 TPM 配额退还给桶（受 capacity 封顶），
避免按 max_tokens 预留导致的长期偏保守。

ReservationLimiterManager 负责按 model_key 提供共享限流器实例
（RPM / TPM 从配置中心读取，实例跨请求复用，同一模型共享同一个桶）。
"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

from app.config import settings


class TokenBucket:
    """
    Token Bucket 限流器（纯 token 记账）。

    - capacity: 桶容量（最大突发请求数）
    - refill_rate: 每秒补充 Token 数

    对外接口：
        - acquire(tokens)    等待配额并扣减
        - refund(tokens)     退还配额（受 capacity 封顶）

    预留/结算语义由上层 Reservation 持有条目、经 refund 结算，桶本身不感知。
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

    async def refund(self, tokens: float = 1.0) -> None:
        """退还 token（受 capacity 封顶，best-effort）。

        请求完成后把未用完的配额退还给桶。容量封顶保证退款不会让
        桶超出 capacity（突发上限不被破坏）。
        """
        async with self._lock:
            self._refill()
            self._tokens = min(self.capacity, self._tokens + tokens)

    def _refill(self) -> None:
        """补充 Token。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now


class Reservation:
    """
    预留的配额，支持事后结算（settle）或取消（cancel）。

    以条目列表统一单桶 / 多桶组合：每个条目 = (桶, 预留量)。
    空对象构造，由 ReservationLimiter.reserve() 逐桶 acquire 扣减后 add() 追加条目。
    语义约定：**首个条目为按次桶（RPM，settle 不退）**——请求已发出即真实消耗；
    其余条目为按量桶（TPM，settle 退差）。单桶场景仅一个条目。

    请求前预留后，请求完成后：
        - settle(actual)    实际消耗 actual，按量桶退 max(0, reserved - actual)
        - settle(None)      保留全部预留（保守），但标记终态
        - cancel()          所有桶全额退还（请求未确认发出时用）

    终态幂等：settle/cancel 任一调用后，再次调用为 no-op。
    """

    __slots__ = ("_entries", "_settled")

    def __init__(self) -> None:
        self._entries: list[tuple[TokenBucket, float]] = []
        self._settled = False

    def add(self, bucket: TokenBucket, reserved: float) -> None:
        """追加一个桶到组合预留（按次桶之后追加按量桶）。"""
        self._entries.append((bucket, reserved))

    async def settle(self, actual: int | None) -> None:
        """按实际消耗结算，退还未使用配额。

        Args:
            actual: 实际消耗的 token 数（None = 保留全部预留，保守语义）

        仅对按量桶（非首个条目）退差；按次桶请求已发出即真实消耗，不退。
        """
        if self._settled:
            return
        self._settled = True
        if actual is None:
            return
        for bucket, reserved in self._entries[1:]:
            await bucket.refund(max(0.0, reserved - actual))

    async def cancel(self) -> None:
        """全额退还所有桶的预留配额（请求未确认发出时用）。幂等。"""
        if self._settled:
            return
        self._settled = True
        for bucket, reserved in self._entries:
            await bucket.refund(reserved)

    @property
    def settled(self) -> bool:
        """是否已到达终态（settle/cancel 任一调用）。"""
        return self._settled


class ReservationLimiter:
    """
    客户端限流器（reserve/settle 形态）。

    RPM + TPM 双 Token Bucket，reserve() 返回组合 Reservation：
        - settle(actual)    退 TPM 差，RPM 不退（请求已发出，RPM 配额真实消耗）
        - cancel()          退 RPM 1 + TPM 全额（请求未确认发出时用）
    """

    def __init__(
        self,
        rpm: int = 60,
        tpm: int = 100_000,
    ) -> None:
        self._req_bucket = TokenBucket(capacity=rpm, refill_rate=rpm / 60)
        self._token_bucket = TokenBucket(capacity=tpm, refill_rate=tpm / 60)

    async def reserve(
        self,
        estimated_tokens: int = 0,
        retry_after: float | None = None,
    ) -> Reservation:
        """预留配额，返回组合 Reservation。

        Args:
            estimated_tokens: 预估的 Token 消耗（TPM 桶按此预留）
            retry_after: 服务端返回的 Retry-After 时间（秒）

        Returns:
            组合 Reservation：settle 退 TPM 差、cancel 退 RPM+TPM
        """
        if retry_after:
            await asyncio.sleep(retry_after)

        est = max(estimated_tokens, 1.0)

        res = Reservation()
        # RPM 预留（固定 1）→ 组合预留的首个条目（按次桶，settle 不退）
        await self._req_bucket.acquire(1.0)
        res.add(self._req_bucket, 1.0)
        try:
            # TPM 预留（按 estimated，防 0 造成桶不扣）→ 追加按量条目
            await self._token_bucket.acquire(est)
        except BaseException:
            # 防 R5：TPM 预留前被硬取消 → 回退已扣的 RPM
            await res.cancel()
            raise
        res.add(self._token_bucket, est)
        return res


# =====================================================================
# 限流器管理
# =====================================================================


# model_key → 读取 settings 中的 RPM / TPM 配置字段名
_RATE_LIMIT_FIELDS: dict[str, tuple[str, str]] = {
    "main": ("llm_main_rpm", "llm_main_tpm"),
    "reasoning": ("llm_reasoning_rpm", "llm_reasoning_tpm"),
    "fast": ("llm_fast_rpm", "llm_fast_tpm"),
}


class ReservationLimiterManager:
    """
    按 model_key 提供共享限流器实例。

    与 ClientManager 同款缓存模式：同一 model_key 复用同一个
    ReservationLimiter（双 Token Bucket 跨请求记账，不能每次 new）。
    配置（RPM / TPM）从配置中心懒加载，修改配置后 reset() 重建。
    """

    _instances: ClassVar[dict[str, ReservationLimiter]] = {}

    @classmethod
    def get(cls, model_key: str = "main") -> ReservationLimiter:
        """获取指定 key 的限流器（懒创建 + 缓存复用）。

        Args:
            model_key: 模型标识（main / reasoning / fast）

        Returns:
            共享 ReservationLimiter 实例

        Raises:
            ValueError: model_key 未在限流配置映射中
        """
        if model_key in cls._instances:
            return cls._instances[model_key]

        fields = _RATE_LIMIT_FIELDS.get(model_key)
        if fields is None:
            raise ValueError(f"未知限流 key: {model_key!r}")

        rpm_field, tpm_field = fields
        limiter = ReservationLimiter(
            rpm=getattr(settings, rpm_field, 0),
            tpm=getattr(settings, tpm_field, 0),
        )
        cls._instances[model_key] = limiter
        return limiter

    @classmethod
    def reset(cls) -> None:
        """清空所有缓存实例（配置变更或测试时调用）。"""
        cls._instances.clear()
