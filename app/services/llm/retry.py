"""
RetryHandler — 增强重试：jitter + circuit breaker + fallback

核心组件：
    RetryConfig      重试配置（最大重试、退避基数、抖动开关）
    CircuitBreaker   熔断器（滑动窗口错误率判定、恢复超时、半开探针）
    RetryHandler     重试执行器（整合退避 + 熔断 + fallback）

熔断判定（工业级，参考 Hystrix 模型）：
    - 滑动时间窗口内统计请求总数与失败数
    - 窗口内总请求 ≥ request_volume_threshold 且 错误率 ≥ error_threshold → 熔断
    - 或 窗口内全部失败且失败数 ≥ all_failed_min → 熔断（低流量纯失败保护）
    - 429（RATE_LIMITED）不计入窗口统计，只退避（尊重服务端 Retry-After）
    - 计数粒度为请求（execute）级：一次 execute 的多次重试只汇报一次结果，
      避免单请求的重试放大熔断计数

使用方式：
    handler = RetryHandler(
        config=RetryConfig(max_retries=3),
        circuit_breaker=CircuitBreaker(),
    )
    result = await handler.execute(
        call_fn=lambda: client.chat.completions.create(**kwargs),
        fallback_fn=lambda: fallback_client.chat.completions.create(**kwargs),
    )
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from openai import APITimeoutError, RateLimitError

from app.config import settings

# =====================================================================
# 重试配置
# =====================================================================


@dataclass
class RetryConfig:
    """重试配置。"""

    max_retries: int = settings.llm_max_retries
    base_delay: float = settings.llm_base_delay
    max_delay: float = settings.llm_max_delay
    use_jitter: bool = settings.llm_use_jitter


# =====================================================================
# 错误分类
# =====================================================================


class ErrorCategory(Enum):
    """错误类型分类，决定处理策略。"""

    RETRYABLE = "retryable"  # 可重试（超时、5xx）
    NON_RETRYABLE = "fatal"  # 不可恢复（认证、参数错误）
    RATE_LIMITED = "rate_limited"  # 限流（可重试但应退避，不计入熔断）


def classify_error(exc: Exception) -> ErrorCategory:
    """对异常进行分类。"""
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        return ErrorCategory.RETRYABLE
    if isinstance(exc, RateLimitError):
        return ErrorCategory.RATE_LIMITED
    # 检查 HTTP status code
    status_code = getattr(exc, "status_code", 0)
    if status_code:
        if 500 <= status_code < 600:
            return ErrorCategory.RETRYABLE
        if status_code == 429:
            return ErrorCategory.RATE_LIMITED
        if status_code in (400, 401, 403, 422):
            return ErrorCategory.NON_RETRYABLE
    return ErrorCategory.RETRYABLE


# =====================================================================
# 熔断器
# =====================================================================


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """
    熔断器（滑动时间窗口 + 错误率判定）。

    判定基于滑动窗口内的请求错误率，参考 Hystrix 工业模型：
        - 窗口内总请求 ≥ request_volume_threshold 且错误率 ≥ error_threshold → OPEN
        - 或 窗口内全部失败且失败数 ≥ all_failed_min → OPEN（低流量纯失败保护）
    429（限流）不计入窗口统计——限流是客户触发自身限额，不是下游故障证据。

    状态机：CLOSED → OPEN → HALF_OPEN → CLOSED / OPEN
    """

    window_seconds: float = settings.llm_circuit_window_seconds
    error_threshold: float = settings.llm_circuit_error_threshold
    request_volume_threshold: int = settings.llm_circuit_request_volume_threshold
    all_failed_min: int = settings.llm_circuit_all_failed_min
    recovery_timeout: float = settings.llm_circuit_recovery_timeout
    half_open_max_requests: int = settings.llm_circuit_half_open_max_requests

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    # 滑动窗口：[(timestamp, is_success), ...]，新条目追加在右，过期从左弹出
    _window: deque[tuple[float, bool]] = field(default_factory=deque, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _half_open_requests: int = field(default=0, init=False)
    _consecutive_successes: int = field(default=0, init=False)

    def allow_request(self) -> bool:
        """判断是否允许请求通过。"""

        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_requests = 1  # 本次作为第一个探针
                return True
            return False

        # HALF_OPEN
        if self._half_open_requests < self.half_open_max_requests:
            self._half_open_requests += 1
            return True
        return False

    # ------------------------------------------------------------------
    # 滑动窗口统计
    # ------------------------------------------------------------------

    def _prune_window(self) -> None:
        """清理窗口内过期的请求记录（惰性，记录操作时调用）。"""
        cutoff = time.monotonic() - self.window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _window_stats(self) -> tuple[int, int]:
        """返回 (窗口内总请求数, 失败数)。"""
        total = len(self._window)
        failures = sum(1 for _, ok in self._window if not ok)
        return total, failures

    def _should_open(self) -> bool:
        """根据窗口统计评估是否触发熔断。"""
        total, failures = self._window_stats()
        if total == 0:
            return False
        # 低流量纯失败保护：请求量不足正常门槛时，全部失败且达最小样本量仍熔断
        if failures == total and failures >= self.all_failed_min:
            return True
        # 主判据：窗口内总请求达到最小请求量，且错误率达标
        if total >= self.request_volume_threshold:
            return failures / total >= self.error_threshold
        return False

    # ------------------------------------------------------------------
    # 结果记录
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """
        记录一次主链路请求成功。
        """

        # OPEN 下：no-op（防御）。正常路径下 OPEN 不会执行 call_fn——
        #  allow_request() 会拒绝它——此状态收到成功只能来自外部误调用，
        #  不得据此关闭熔断器；恢复只能由主链路探针验证。
        if self._state == CircuitState.OPEN:
            return

        # HALF_OPEN 下：累积连续探针成功，达到探针阈值才关闭熔断器
        if self._state == CircuitState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.half_open_max_requests:
                self._state = CircuitState.CLOSED
                self._window.clear()
                self._half_open_requests = 0
                self._consecutive_successes = 0
            return

        # CLOSED 下：窗口追加成功，错误率随窗口滑动自然回落
        self._prune_window()
        self._window.append((time.monotonic(), True))

    def record_failure(self) -> bool:
        """
        记录一次主链路请求失败，更新熔断状态。

        调用方（RetryHandler）在**整个请求触及过 RETRYABLE 故障**（任一次
        尝试是超时/5xx）时才调用本方法；纯 429（限流）/ 不可恢复错误不调用
        本方法——限流只退避、后者是调用方问题，均非下游故障证据。

        Returns:
            True 表示本次失败将熔断器切换到 OPEN——调用方可据此感知
            熔断已触发（请求粒度下熔断评估在请求完成后进行）。
        """
        # OPEN 下防御：正常路径不可达，即便外部误调用也不累计窗口、
        # 不改写冷却计时——熔断期间窗口统计保持冻结。
        if self._state == CircuitState.OPEN:
            return False

        if self._state == CircuitState.HALF_OPEN:
            # 探针失败 → 重新熔断，新一轮冷却开始
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            self._consecutive_successes = 0
            return True

        # CLOSED 下：窗口追加失败并评估熔断
        self._prune_window()
        self._window.append((time.monotonic(), False))
        if self._should_open():
            # CLOSED → OPEN（冷却期起点）
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            return True

        # CLOSED 下未达熔断条件 → 不改写冷却计时
        return False

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        """窗口内当前失败请求数（先清理过期条目）。"""
        self._prune_window()
        return sum(1 for _, ok in self._window if not ok)

    def reset(self) -> None:
        """手动重置熔断器。"""
        self._state = CircuitState.CLOSED
        self._window.clear()
        self._half_open_requests = 0
        self._consecutive_successes = 0


# =====================================================================
# 增强重试执行器
# =====================================================================


class CircuitBreakerOpenError(Exception):
    """熔断器开启时请求被拒绝。"""


class RetryHandler:
    """
    带熔断和 fallback 的重试执行器。

    用法：
        handler = RetryHandler()
        result = await handler.execute(
            call_fn=lambda: client.chat.completions.create(**kwargs),
            fallback_fn=lambda: fallback_client.chat.completions.create(**kwargs),
        )
    """

    def __init__(
        self,
        config: RetryConfig | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        self.config = config or RetryConfig()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

    async def execute(
        self,
        call_fn: Callable[[], Awaitable[Any]],
        fallback_fn: Callable[[], Awaitable[Any]] | None = None,
    ) -> Any:
        """
        执行调用，自动重试和熔断。

        熔断器只观察主链路（call_fn）的成败。fallback 是纯兜底，其成败
        不进入熔断状态机——备用链路的健康不代表主链路状态，反之亦然。

        计数粒度为请求级：一次 execute 的多次重试只向熔断器汇报一次结果
        （成功在循环内记录，失败在循环结束后统一记录一次），避免单请求的
        重试放大熔断计数。

        Args:
            call_fn: 主要调用函数
            fallback_fn: 降级调用函数（主调用全部失败时尝试）

        Returns:
            API 响应

        Raises:
            最后一次异常（所有重试 + fallback 均失败）
        """
        last_exc: Exception | None = None
        cb = self.circuit_breaker
        # 本次请求是否出现过下游故障（超时/5xx，RETRYABLE）。
        # 熔断判定看"整个请求是否触及下游故障"，而非只看最后一次异常——
        # 一次请求混合 429 与超时时，只要任一次尝试是超时/5xx 就应计入窗口。
        saw_retryable_failure = False

        # --- 熔断检查 ---
        if not cb.allow_request():
            # 熔断 OPEN，或半开探针占满：拒绝主调用。若有 fallback，
            # 走纯兜底（单次、不重试、不进入熔断状态机）保证服务不中断。
            # fallback 失败时其异常自然向上传播（调用方仅拿到 fallback
            # 的异常，不再附加熔断信息）。
            if fallback_fn is not None:
                return await fallback_fn()
            raise CircuitBreakerOpenError(
                f"熔断器已开启，拒绝请求。"
                f"状态: {cb.state.value}, "
                f"窗口内失败: {cb.failure_count}"
            )

        # --- 半开探针：单次调用主链路，不做重试 ---
        # 恢复探测只放行一次请求：探针失败即确认未恢复、回到 OPEN，
        # 若探针也能重试，一次探测失败会被放大成多次调用（甚至误判恢复）。
        if cb.state == CircuitState.HALF_OPEN:
            return await self._probe_attempt(call_fn, fallback_fn=fallback_fn)

        # --- 重试循环（仅 CLOSED 正常状态）---
        for attempt in range(self.config.max_retries + 1):
            try:
                result = await call_fn()
                cb.record_success()
                return result
            except Exception as e:
                last_exc = e
                category = classify_error(e)

                # 不可恢复的错误 → 直接抛出（不计入熔断窗口）
                if category == ErrorCategory.NON_RETRYABLE:
                    raise

                # 出现下游故障（超时/5xx）：标记本次请求触及过故障
                if category == ErrorCategory.RETRYABLE:
                    saw_retryable_failure = True

                # 最后一次尝试失败 → 结束循环，进入请求级记录 + fallback
                if attempt >= self.config.max_retries:
                    break

                # 限流：尊重服务端退避时间（Retry-After），不计入熔断。
                # 可重试（超时/5xx）：按指数退避等待后重试。
                retry_after = (
                    self._extract_retry_after(e)
                    if category == ErrorCategory.RATE_LIMITED
                    else None
                )
                delay = self._calculate_delay(attempt, retry_after=retry_after)
                await asyncio.sleep(delay)

        # --- 请求粒度统一记录主链路最终结果 ---
        # 本次请求只要任一次尝试是 RETRYABLE 故障（超时/5xx），就记录一次失败
        # （计入下游故障信号）。纯 429 / 不可恢复错误不计入熔断窗口：
        # 前者只退避、后者是调用方问题，均非下游故障证据。
        if saw_retryable_failure:
            cb.record_failure()

        # --- fallback（纯兜底，不进入熔断状态机）---
        # 主链路已按自身成败记录过熔断（record_success/record_failure）；
        # fallback 是备用链路，成功/失败都不再触碰熔断器。
        if fallback_fn is not None:
            try:
                return await fallback_fn()
            except Exception as e:
                last_exc = e

        # --- 所有路径均失败 ---
        assert last_exc is not None
        raise last_exc

    async def _probe_attempt(
        self,
        call_fn: Callable[[], Awaitable[Any]],
        fallback_fn: Callable[[], Awaitable[Any]] | None = None,
    ) -> Any:
        """
        半开探针：单次调用主链路验证恢复。

        探针（主链路）的成败进入熔断状态机：成功累计探针成功数，失败使
        熔断器回 OPEN（新一轮冷却）。429（限流）不计入——限流不代表未
        恢复，本次探测不改变熔断状态。fallback 纯兜底返回给用户，
        不触碰熔断器。
        """
        cb = self.circuit_breaker
        last_exc: Exception | None = None

        try:
            result = await call_fn()
            cb.record_success()
            return result
        except Exception as e:
            last_exc = e
            category = classify_error(e)
            if category not in (
                ErrorCategory.NON_RETRYABLE,
                ErrorCategory.RATE_LIMITED,
            ):
                cb.record_failure()

        # 探针失败：尝试 fallback 兜底（不记录熔断）
        if fallback_fn is not None:
            try:
                return await fallback_fn()
            except Exception as e:
                last_exc = e

        assert last_exc is not None
        raise last_exc

    def _calculate_delay(
        self,
        attempt: int,
        retry_after: float | None = None,
    ) -> float:
        """计算退避延迟（指数 + 可选随机抖动，429 时尊重服务端 Retry-After）。"""
        delay = self.config.base_delay * (2**attempt)
        delay = min(delay, self.config.max_delay)
        if self.config.use_jitter:
            delay = random.uniform(0, delay)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    @staticmethod
    def _extract_retry_after(exc: Exception) -> float | None:
        """从限流异常中提取 Retry-After 秒数（无则返回 None）。"""
        headers = getattr(exc, "headers", None)
        if not headers:
            return None
        value = headers.get("retry-after") or headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except TypeError, ValueError:
            return None  # 可能是 HTTP-date 形式，简化忽略，回退到指数退避
