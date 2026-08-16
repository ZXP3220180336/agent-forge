"""
ReservationLimiter — 客户端限流（reserve/settle 形态）

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
（RPM / TPM 由外层 register_config() 注入，实例跨请求复用，同一模型共享同一个桶）。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from app.platform.observability.logger import get_logger

logger = get_logger("llm.reservation_limiter")


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

        # 单次请求超过桶容量：截断到 capacity，避免 while True 无限等待。
        # _refill 将 _tokens 封顶于 capacity，tokens > capacity 时 _tokens >= tokens
        # 永假（桶永远装不满一个超容量请求）——不截断则死循环。截断语义：
        # 该请求一次占满桶容量（突发上限），不拒绝不等待，由上层按实际结算退差。
        tokens = min(tokens, self.capacity)

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


class OutputTokenEstimator:
    """
    输出 token 自适应估算器（Fenic 式）。

    维护历史实际输出的滚动样本，用「高分位 × 安全系数」预测下一次预留的输出量，
    替代固定 max_tokens 上限——减少预留期间占桶（并发空耗）。

    - quantile: 分位数（普通模型 p95，推理模型 p99——推理输出有相关性突发尖峰）
    - safety_margin: 安全系数（默认 1.15，越高越保守、429 风险越低、吞吐越低）
    - min_samples: 冷启动阈值——样本不足返回 0，调用方回退静态上限
    - window: 滚动样本窗口（deque 上限，超限淘汰最旧）

    线程/异步安全：record/estimate 均无 await 点，asyncio 单线程下天然原子，
    无需锁（与 TokenBucket 因 acquire 内 sleep 才需锁不同）。
    """

    def __init__(
        self,
        quantile: float = 0.95,
        safety_margin: float = 1.15,
        min_samples: int = 30,
        window: int = 256,
    ) -> None:
        self.quantile = quantile
        self.safety_margin = safety_margin
        self.min_samples = min_samples
        self._samples: deque[int] = deque(maxlen=window)

    def record(self, actual_output_tokens: int) -> None:
        """记录一次实际输出 token（settle 成功后喂入）。"""
        self._samples.append(max(0, int(actual_output_tokens)))

    def estimate(self) -> int:
        """返回高分位 × 安全系数的预测输出量；冷启动（样本不足）返回 0。

        返回 0 时调用方应回退静态上限（max_tokens）。
        """
        if len(self._samples) < self.min_samples:
            return 0
        ordered = sorted(self._samples)
        # quantile clamp 到 [0, 1]：负值（配置异常）会取负索引倒数元素、语义错反
        q = max(0.0, min(1.0, self.quantile))
        idx = min(int(q * (len(ordered) - 1)), len(ordered) - 1)
        return math.ceil(ordered[idx] * self.safety_margin)

    def reset(self) -> None:
        """清空所有样本（配置变更或测试时调用）。"""
        self._samples.clear()


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

    __slots__ = ("_entries", "_settle_callback", "_settled", "_lock")

    def __init__(self, settle_callback: Callable[[int], None] | None = None) -> None:
        self._entries: list[tuple[TokenBucket, float]] = []
        self._settled = False
        # settle(actual) 成功路径的回调：喂实际消耗给自适应估算器（可选）。
        # 仅当 actual 非 None（有真实 usage）时触发；settle(None)/cancel 不触发。
        self._settle_callback = settle_callback
        # LLM-010：终态操作（settle/cancel）互斥锁——「_settled 检查 + 退款循环」
        # 含 await 点，无锁时并发调用可同时通过检查重复退款，向桶注入不存在的额度。
        self._lock = asyncio.Lock()

    def add(self, bucket: TokenBucket, reserved: float) -> None:
        """追加一个桶到组合预留（按次桶之后追加按量桶）。"""
        self._entries.append((bucket, reserved))

    async def settle(self, actual: int | None) -> None:
        """按实际消耗结算，退还未使用配额。

        Args:
            actual: 实际消耗的 token 数（None = 保留全部预留，保守语义）

        仅对按量桶（非首个条目）退差；按次桶请求已发出即真实消耗，不退。
        """
        async with self._lock:  # LLM-010：终态操作互斥，防并发重复退款
            if self._settled:
                return
            if actual is not None:
                for bucket, reserved in self._entries[1:]:
                    await bucket.refund(max(0.0, reserved - actual))
            # 终态标记放在全部退款完成之后：退款循环中途被取消时（CancelledError
            # 向上传播）保持未终态，外层兜底（llm_service 的 cancel / finally）可
            # 续退其余条目，避免部分桶配额永久泄漏。TokenBucket.refund 的 capacity
            # 封顶保证重复退款安全，不超发。
            self._settled = True
            if self._settle_callback is not None and actual is not None:
                self._settle_callback(actual)

    async def cancel(self) -> None:
        """全额退还所有桶的预留配额（请求未确认发出时用）。幂等。"""
        async with self._lock:  # LLM-010：终态操作互斥，防并发重复退款
            if self._settled:
                return
            for bucket, reserved in self._entries:
                await bucket.refund(reserved)
            self._settled = True

    @property
    def settled(self) -> bool:
        """是否已到达终态（settle/cancel 任一调用）。"""
        return self._settled


@dataclass
class ReservationLimiterConfig:
    """ReservationLimiter 配置（纯配置对象，默认值为合理硬编码；由外层
    ReservationLimiterManager.register_config() 注入）。"""

    rpm: int = 60
    tpm: int = 100_000
    quantile: float = 0.95
    safety_margin: float = 1.15
    min_samples: int = 30
    window: int = 256


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
        *,
        quantile: float = 0.95,
        safety_margin: float = 1.15,
        min_samples: int = 30,
        window: int = 256,
    ) -> None:
        self._req_bucket = TokenBucket(capacity=rpm, refill_rate=rpm / 60)
        self._token_bucket = TokenBucket(capacity=tpm, refill_rate=tpm / 60)
        # 自适应预留：按 max_tokens 键控的输出估算器池（懒创建）
        self._quantile = quantile
        self._safety_margin = safety_margin
        self._min_samples = min_samples
        self._window = window
        self._estimators: dict[int, OutputTokenEstimator] = {}

    async def _acquire(
        self,
        estimated_tokens: float,
        retry_after: float | None,
        settle_callback: Callable[[int], None] | None = None,
    ) -> Reservation:
        """预留配额的核心逻辑（RPM + TPM 双桶 + 防 R5）。"""
        if retry_after:
            await asyncio.sleep(retry_after)

        est = max(estimated_tokens, 1.0)

        # 单请求预估超过 TPM 桶容量：截断到 capacity 并记 warning。
        # 触发：超长 prompt + 小 tpm 配置（或 llm_adaptive_reserve 的 prompt 无上限）。
        # 底层 TokenBucket.acquire 已对超容量做截断兜底，此处显式截断是为了让
        # res.add 记录的实际预留值与扣减一致（settle 退差基础正确），并暴露配置问题。
        tpm_capacity = self._token_bucket.capacity
        if est > tpm_capacity:
            logger.warning(
                "TPM 预留 %s 超过桶容量 %s，已截断到容量——请检查 llm_*_tpm 配置"
                "是否过小，或单次请求 token 预估是否异常",
                est,
                tpm_capacity,
            )
            est = tpm_capacity

        res = Reservation(settle_callback=settle_callback)
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

    async def reserve(
        self,
        estimated_tokens: int = 0,
        retry_after: float | None = None,
    ) -> Reservation:
        """预留配额（固定形态），返回组合 Reservation。

        Args:
            estimated_tokens: 预估的 Token 消耗（TPM 桶按此预留）
            retry_after: 服务端返回的 Retry-After 时间（秒）

        Returns:
            组合 Reservation：settle 退 TPM 差、cancel 退 RPM+TPM
        """
        return await self._acquire(estimated_tokens, retry_after)

    async def reserve_adaptive(
        self,
        prompt_tokens: int,
        max_tokens: int,
        retry_after: float | None = None,
    ) -> Reservation:
        """预留配额（自适应形态）：用高分位估算输出，减少预留期间占桶。

        Args:
            prompt_tokens: 本次请求的 prompt token 数
            max_tokens: 输出上限（预留 clamp 上限，provider 仍收到此值不截断）
            retry_after: 服务端返回的 Retry-After 时间（秒）

        Returns:
            组合 Reservation；settle 时把实际输出喂给对应 max_tokens 的估算器池。
        """
        completion_est = self._estimate_completion(max_tokens)
        est = prompt_tokens + completion_est  # 恒 ≤ prompt + max_tokens（clamp）
        return await self._acquire(
            est,
            retry_after,
            settle_callback=lambda actual_total: self._record_actual(
                prompt_tokens, max_tokens, actual_total
            ),
        )

    def _estimate_completion(self, max_tokens: int) -> int:
        """估算本次请求的输出 token 量（高分位 × 安全系数，clamp 到 max_tokens）。

        冷启动（样本 < min_samples）返回 0 → 调用方回退静态上限（max_tokens）。
        """
        est = self._estimator_for(max_tokens).estimate()
        if est <= 0:
            return max_tokens  # 冷启动回退静态上限
        return min(est, max_tokens)  # clamp：只减不加（Fenic 关键原则）

    def _record_actual(
        self, prompt_tokens: int, max_tokens: int, actual_total: int
    ) -> None:
        """settle 回调：把实际输出 token 喂给估算器池。

        actual_total 是 usage.total_tokens（prompt + completion），
        实际输出 = total - prompt（与预留时的 prompt 口径一致）。
        """
        completion = max(0, actual_total - prompt_tokens)
        self._estimator_for(max_tokens).record(completion)

    def _estimator_for(self, max_tokens: int) -> OutputTokenEstimator:
        """按 max_tokens 懒创建/复用输出估算器池。"""
        est = self._estimators.get(max_tokens)
        if est is None:
            est = OutputTokenEstimator(
                quantile=self._quantile,
                safety_margin=self._safety_margin,
                min_samples=self._min_samples,
                window=self._window,
            )
            self._estimators[max_tokens] = est
        return est


# =====================================================================
# 限流器管理
# =====================================================================


class ReservationLimiterManager:
    """
    按 model_key 提供共享限流器实例。

    与 ClientManager 同款缓存模式：同一 model_key 复用同一个
    ReservationLimiter（双 Token Bucket 跨请求记账，不能每次 new）。
    配置（RPM / TPM）由外层 register_config() 注入（Container 读 settings 后调用），
    子模块不再直接依赖 settings；修改配置后 reset() 重建实例生效。
    """

    _instances: ClassVar[dict[str, ReservationLimiter]] = {}
    _configs: ClassVar[dict[str, ReservationLimiterConfig]] = {}

    @classmethod
    def register_config(cls, configs: dict[str, ReservationLimiterConfig]) -> None:
        """注入按 model_key 的限流配置，并重建已缓存实例使新配置生效。

        Args:
            configs: model_key → ReservationLimiterConfig 映射
        """
        cls._configs = dict(configs)
        cls.reset()

    @classmethod
    def get(cls, model_key: str = "main") -> ReservationLimiter:
        """获取指定 key 的限流器（懒创建 + 缓存复用）。

        model_key 由外部传入（与 ClientManager 对齐，不内置白名单）：
        任意 key 都懒构建——未配置过的 key 用默认 ReservationLimiter。

        Args:
            model_key: 模型标识（如 main / reasoning / fast / 自定义）

        Returns:
            共享 ReservationLimiter 实例
        """
        if model_key in cls._instances:
            return cls._instances[model_key]
        limiter = cls._build_for(model_key)
        cls._instances[model_key] = limiter
        return limiter

    @classmethod
    def _build_for(cls, model_key: str) -> ReservationLimiter:
        """按注入配置构建指定 key 的限流器；未配置的 key 用默认值。"""
        config = cls._configs.get(model_key)
        if config is None:
            return ReservationLimiter()
        return ReservationLimiter(
            rpm=config.rpm,
            tpm=config.tpm,
            quantile=config.quantile,
            safety_margin=config.safety_margin,
            min_samples=config.min_samples,
            window=config.window,
        )

    @classmethod
    def reset(cls) -> None:
        """清空所有缓存实例（配置变更或测试时调用）。"""
        cls._instances.clear()
