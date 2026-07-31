"""
RetryHandler — 增强重试：jitter + circuit breaker + fallback

核心组件：
    RetryConfig     重试配置（最大重试、退避基数、抖动开关）
    CircuitBreaker  熔断器（连续失败阈值、恢复超时、半开探针）
    RetryHandler    重试执行器（整合退避 + 熔断 + fallback）

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
    RATE_LIMITED = "rate_limited"  # 限流（可重试但应标记熔断）


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
    熔断器。

    连续失败达到阈值 → 开启熔断 → 拒绝请求
    超时后进入半开 → 放行探针 → 成功则关闭，失败则继续熔断
    """

    failure_threshold: int = settings.llm_circuit_failure_threshold
    recovery_timeout: float = settings.llm_circuit_recovery_timeout
    half_open_max_requests: int = settings.llm_circuit_half_open_max_requests

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
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

    def record_success(self) -> None:
        """
        记录一次主链路成功。
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
                self._failure_count = 0
                self._half_open_requests = 0
                self._consecutive_successes = 0
            return

        # CLOSED 下：重置失败计数
        if self._state == CircuitState.CLOSED:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_requests = 0
            self._consecutive_successes = 0

    def record_failure(self) -> bool:
        """
        记录一次主链路失败，更新熔断状态。

        Returns:
            True 表示本次失败将熔断器切换到 OPEN——调用方应立即停止剩余重试，
            不再对已确认故障的下游发无用请求。
        """
        # OPEN 下防御：正常路径不可达，即便外部误调用也不累计失败计数、
        # 不改写冷却计时——熔断期间 _failure_count 保持冻结。
        if self._state == CircuitState.OPEN:
            return False

        if self._state == CircuitState.HALF_OPEN:
            # 探针失败 → 重新熔断，新一轮冷却开始
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            self._consecutive_successes = 0
            return True

        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            # CLOSED → OPEN（冷却期起点）
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            return True

        # CLOSED 下未达阈值 → 不改写冷却计时
        return False

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def reset(self) -> None:
        """手动重置熔断器。"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
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
                f"连续失败: {cb.failure_count}"
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

                # 不可恢复的错误 → 直接抛出
                if category == ErrorCategory.NON_RETRYABLE:
                    raise

                # 可重试和限流错误 → 计入熔断计数。
                # record_failure 返回 True 表示已触发 OPEN → 立即停止剩余重试，
                # 不再对已确认故障的下游发无用请求。
                if cb.record_failure():
                    break

                # 最后一次尝试失败 → 尝试 fallback
                if attempt >= self.config.max_retries:
                    break

                # 等待后重试
                delay = self._calculate_delay(attempt)
                await asyncio.sleep(delay)

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
        熔断器回 OPEN（新一轮冷却）。fallback 纯兜底返回给用户，不触碰熔断器。
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
            if category != ErrorCategory.NON_RETRYABLE:
                cb.record_failure()

        # 探针失败：尝试 fallback 兜底（不记录熔断）
        if fallback_fn is not None:
            try:
                return await fallback_fn()
            except Exception as e:
                last_exc = e

        assert last_exc is not None
        raise last_exc

    def _calculate_delay(self, attempt: int) -> float:
        """计算退避延迟（指数 + 可选随机抖动）。"""
        delay = self.config.base_delay * (2**attempt)
        delay = min(delay, self.config.max_delay)
        if self.config.use_jitter:
            delay = random.uniform(0, delay)
        return delay
