"""
RetryHandler — 增强重试：jitter + circuit breaker + fallback

核心组件：
    RetryConfig             重试配置（最大重试、退避基数、抖动开关）
    CircuitBreaker          熔断器（滑动窗口错误率判定、恢复超时、半开探针）
    RetryHandler            重试执行器（整合退避 + 熔断 + fallback）
    RetryHandlerManager     重试执行器管理（按 model_key 提供共享 RetryHandler 实例）

使用方式：
    retry = RetryHandlerManager.get(model_key)
    response = await retry.execute(
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
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

import httpx
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APITimeoutError,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    RateLimitError,
)

# =====================================================================
# 错误分类
# =====================================================================


class ErrorCategory(Enum):
    """错误类型分类，决定处理策略。"""

    RETRYABLE = "retryable"  # 可重试（超时、5xx）
    NON_RETRYABLE = "fatal"  # 不可恢复（认证、参数错误）
    RATE_LIMITED = "rate_limited"  # 限流（可重试但应退避，不计入熔断）


# 可重试的具名异常：网络层故障（超时、连接错误）。无 status_code，必须显式匹配。
_RETRYABLE_EXC = (TimeoutError, APITimeoutError, APIConnectionError)
# 非 HTTP 的永久性异常：响应校验失败、token 截断、内容被过滤——重试无效。
_NON_RETRYABLE_EXC = (
    APIResponseValidationError,
    LengthFinishReasonError,
    ContentFilterFinishReasonError,
)


def classify_error(exc: Exception) -> ErrorCategory:
    """对异常进行分类（白名单映射，未知异常默认不可重试）。

    分类规则：
        - RETRYABLE    网络层故障（openai 封装或裸 httpx）、超时、5xx
        - RATE_LIMITED 429
        - NON_RETRYABLE 4xx、响应校验错误、token 截断、内容被过滤、
                        以及未知异常（默认兜底——避免对重试无效的错误盲目重试）
    """
    # 1) 网络层：openai 封装（APITimeoutError / APIConnectionError）+ 裸 httpx 异常
    #    openai 某些路径会直接抛 httpx 异常（ConnectError/ReadError/Timeout 等），不会被封装。
    #    httpx.TimeoutException 与 httpx.NetworkError 无继承关系，需同时匹配。
    if isinstance(
        exc,
        _RETRYABLE_EXC + (httpx.TimeoutException, httpx.NetworkError),
    ):
        return ErrorCategory.RETRYABLE
    # 2) 限流
    if isinstance(exc, RateLimitError):
        return ErrorCategory.RATE_LIMITED
    # 3) HTTP 状态码（APIStatusError 及其子类都带 status_code）
    status_code = getattr(exc, "status_code", 0)
    if status_code:
        if 500 <= status_code < 600:
            return ErrorCategory.RETRYABLE
        if status_code == 429:
            return ErrorCategory.RATE_LIMITED
        if 400 <= status_code < 500:
            return ErrorCategory.NON_RETRYABLE
    # 4) 明确的非 HTTP 永久性异常
    if isinstance(exc, _NON_RETRYABLE_EXC):
        return ErrorCategory.NON_RETRYABLE
    # 5) 未知异常：默认不可重试（避免对无法恢复的错误盲目重试打下游）
    return ErrorCategory.NON_RETRYABLE


# =====================================================================
# 熔断器
# =====================================================================


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """熔断器配置（纯配置对象，默认值为合理硬编码；运行时由 RetryHandlerManager
    register_config() 注入 settings 值）。"""

    window_seconds: float = 10.0
    error_threshold: float = 0.5
    request_volume_threshold: int = 20
    all_failed_min: int = 3
    recovery_timeout: float = 30.0
    half_open_max_requests: int = 3


class CircuitBreaker:
    """
        熔断器（滑动窗口时间 + 错误率判定）。

    熔断判定（工业级：基于滑动窗口内的请求错误率判定，参考 Hystrix 模型）：
        - 滑动时间窗口内统计请求总数与失败数
        - 窗口内总请求 ≥ request_volume_threshold 且 错误率 ≥ error_threshold → 熔断
        - 或 窗口内全部失败且失败数 ≥ all_failed_min → 熔断（低流量纯失败保护）
        - 429（RATE_LIMITED）不计入窗口统计，只退避（尊重服务端 Retry-After）
          限流是客户触发自身限额，不是下游故障证据
        - 计数粒度为请求（execute）级：一次 execute 的多次重试只汇报一次结果，
          避免单请求的重试放大熔断计数

        配置统一由 CircuitBreakerConfig 承载（配置对象，纯数据），本类只持有
        config 并维护状态机。状态机：CLOSED → OPEN → HALF_OPEN → CLOSED / OPEN
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        # 滑动窗口：[(timestamp, is_success), ...]，新条目追加在右，过期从左弹出
        self._window: deque[tuple[float, bool]] = deque()
        self._last_failure_time = 0.0
        self._half_open_requests = 0
        self._consecutive_successes = 0

    def allow_request(self) -> bool:
        """判断是否允许请求通过。"""

        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            if (
                time.monotonic() - self._last_failure_time
                >= self.config.recovery_timeout
            ):  # 冷却期结束，进入半开探针阶段
                self._state = CircuitState.HALF_OPEN
                self._half_open_requests = 1  # 本次作为第一个探针
                return True
            return False

        # HALF_OPEN
        if self._half_open_requests < self.config.half_open_max_requests:
            self._half_open_requests += 1
            return True
        return False

    def release_probe(self) -> None:
        """半开探针归还槽位，保证槽位永不泄漏：本次探测不计入健康判定时调用。

        槽位泄露后果：
        - 泄漏会让 HALF_OPEN 下 _half_open_requests 被永久占用，allow_request
        恒为 False，熔断器自动恢复失效。

        归还槽位的场景：
        - 探针收到 NON_RETRYABLE（4xx/参数/权限/未知）客户端问题：不代表下游状态。
        - 探针收到任何未记账的退出路径（SystemExit / KeyboardInterrupt / 自定义
        BaseException，或未来新增的异常分支遗漏）

        归还槽位的语义：
        - 不 record_failure（避免 HALF_OPEN→OPEN 反复横跳，
        即客户端错误把熔断器反复打回 OPEN，下游即使恢复也无法完成健康探测），
        - 不 record_success（4xx 不算健康探测），
        - 归还探针槽位让后续正常请求探测真实状态；

        归还槽位的限制：
        - 只在 HALF_OPEN 下有效：若状态已变（如并发探针已回 OPEN），槽位记账已冻结，
        无需递减——下次 OPEN→HALF_OPEN 时由 allow_request() 重置。
        - _consecutive_successes 保持不变（此前成功的健康探测仍有效）。
        """
        if self._state == CircuitState.HALF_OPEN and self._half_open_requests > 0:
            self._half_open_requests -= 1

    # ------------------------------------------------------------------
    # 滑动窗口统计
    # ------------------------------------------------------------------

    def _prune_window(self) -> None:
        """清理窗口内过期的请求记录（惰性，记录操作时调用）。"""
        cutoff = time.monotonic() - self.config.window_seconds
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
        if failures == total and failures >= self.config.all_failed_min:
            return True
        # 主判据：窗口内总请求达到最小请求量，且错误率达标
        if total >= self.config.request_volume_threshold:
            return failures / total >= self.config.error_threshold
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
            if self._consecutive_successes >= self.config.half_open_max_requests:
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

        - RETRYABLE（超时/5xx）：**整个请求过程中，只要出现一次**，
        必须调用本方法计入熔断窗口，可能触发熔断。
        - RATE_LIMITED（429）：熔断Close情况下限流只退避，不计入熔断窗口；
        熔断Half_Open情况下限流是下游过载信号，仍会触发熔断打开。
        - NON_RETRYABLE（4xx/参数/权限/未知）：客户端问题，任何情况下都不调用本方法。

        Returns:
            True 表示本次失败把熔断器切到 OPEN。**当前无调用方消费该返回值**
            （请求级记账：熔断评估在重试循环结束后统一进行，单请求内不打断
            剩余重试，见 retry.md 改造记录「问题 2」）——保留作语义标记，
            供未来请求级/半开优化使用（LLM-013）。
        """
        # OPEN 下no-op防御：正常路径不可达，即便外部误调用也不累计窗口、
        # 不改写冷却计时——熔断期间窗口统计保持冻结。
        if self._state == CircuitState.OPEN:
            return False

        if self._state == CircuitState.HALF_OPEN:
            # 探针失败 → 重新熔断，新一轮冷却开始。
            # 清零半开计数与连续成功：OPEN 状态下不残留 HALF_OPEN 的记账
            # （冷却后重新放行时由 allow_request() 重置为 1，但语义上 OPEN
            #  不应再持有"半开计数"）。
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
            self._consecutive_successes = 0
            self._half_open_requests = 0
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
# 重试配置
# =====================================================================


@dataclass
class RetryConfig:
    """重试配置（纯配置对象，默认值为合理硬编码；运行时由 RetryHandlerManager
    register_config() 注入 settings 值）。"""

    max_retries: int = 2
    base_delay: float = 1.0
    max_delay: float = 30.0
    use_jitter: bool = True


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
        # 恢复探测只放行一次请求：探针失败（429/超时/5xx）即确认未恢复、回到
        # OPEN，若探针也能重试，一次探测失败会被放大成多次调用（甚至误判恢复）。
        # 4xx 探针不改变状态（见 _probe_attempt），归还槽位等待正常请求。
        if cb.state == CircuitState.HALF_OPEN:
            return await self._probe_attempt(call_fn, fallback_fn=fallback_fn)

        # --- 重试循环（仅 CLOSED 正常状态）---
        for attempt in range(self.config.max_retries + 1):
            try:
                result = await call_fn()
                cb.record_success()
                return result
            except asyncio.CancelledError:
                # 退避 sleep 期间被硬取消：本次请求已触及过 RETRYABLE 故障
                # （5xx/超时）的话，该下游故障信号仍应计入熔断窗口——取消是
                # 客户端主动终止，不代表下游恢复，故障证据不能随取消丢失。
                if saw_retryable_failure:
                    cb.record_failure()
                raise
            except Exception as e:
                last_exc = e
                category = classify_error(e)

                # 不可恢复的错误 → 直接抛出。但若本次请求此前已触及过
                # RETRYABLE 故障（超时/5xx），该下游故障信号仍应计入熔断窗口
                # ——4xx 本身是调用方问题不计入，但前期的下游故障不能因
                # 最后一次是 4xx 而被抹掉。
                if category == ErrorCategory.NON_RETRYABLE:
                    if saw_retryable_failure:
                        cb.record_failure()
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
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    # 退避 sleep 期间被硬取消：本次请求已触及过 RETRYABLE 故障
                    # （5xx/超时）的话，下游故障信号仍应计入熔断窗口——取消是
                    # 客户端主动终止，不代表下游恢复，故障证据不能随取消丢失。
                    if saw_retryable_failure:
                        cb.record_failure()
                    raise

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
            except Exception as fallback_exc:
                # 主调用异常才是最终结果（上层按它判定熔断/重试语义，
                # 熔断窗口记录的也是主链路）；fallback 失败仅作为 __cause__ 链上，
                # 不覆盖主异常——否则上层拿到 fallback 异常会与熔断器记录的
                # 主链路状态不一致。
                assert last_exc is not None  # 走到 fallback 必然主调用已失败
                raise last_exc from fallback_exc

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

        探针（主链路）的成败进入熔断状态机：
            - 成功（2xx）→ 累计探针成功数，达到阈值关闭熔断器
            - 429（RATE_LIMITED）→ 下游仍过载，熔断器回 OPEN（新一轮冷却，
              停止探测让下游喘息）
            - 超时/5xx（RETRYABLE）→ 下游仍故障，熔断器回 OPEN（新一轮冷却）
            - 4xx / 未知异常（NON_RETRYABLE）→ **不改变熔断状态、不记录失败**，
              归还探针槽位（release_probe）后异常直接抛给上层——这是客户端问题
              （配置/参数/权限），不代表下游健康状态；本次探测无效不算健康探测，
              释放名额让后续正常请求探测真实状态，熔断器保持在 HALF_OPEN，
              不会因客户端错误在 HALF_OPEN→OPEN 间反复横跳
        fallback 纯兜底返回给用户，不触碰熔断器。
        """
        cb = self.circuit_breaker
        last_exc: Exception | None = None
        # 探针槽位是否已作出终态记账（record_success / record_failure / release_probe）。
        # 未记账的退出路径由 finally 兜底归还槽位，保证槽位永不泄漏。
        accounted = False

        try:
            result = await call_fn()
            cb.record_success()
            accounted = True
            return result
        except asyncio.CancelledError:
            # 探针被取消：槽位已在 allow_request 时占用（+1），需归还。
            # 取消不提供任何下游健康证据——按失败处理回 OPEN（下一轮冷却后
            # 重新探测，避免取消风暴持续占用探针槽位卡死 HALF_OPEN），
            # 并立即向上传播取消，不尝试 fallback（外部取消不应继续发请求）。
            cb.record_failure()
            accounted = True
            raise
        except Exception as e:
            last_exc = e
            category = classify_error(e)
            if category == ErrorCategory.NON_RETRYABLE:
                # 客户端问题（4xx/参数/权限/未知）：不代表下游状态。
                # 归还探针槽位并将异常直接抛给上层修复请求。
                cb.release_probe()
                accounted = True
                raise
            # 429 / 超时 / 5xx：下游故障（过载/无响应）证据，熔断器回 OPEN
            cb.record_failure()
            accounted = True
        finally:
            # 兜底：任何未记账的退出路径（SystemExit / KeyboardInterrupt / 自定义
            # BaseException，或未来新增的异常分支遗漏）归还探针槽位
            if not accounted:
                cb.release_probe()

        # 探针失败（429/超时/5xx）：尝试 fallback 兜底（不记录熔断）
        if fallback_fn is not None:
            try:
                return await fallback_fn()
            except Exception as fallback_exc:
                # 主调用（探针）异常才是最终结果：熔断窗口已按主链路记录
                # （record_failure 回 OPEN），上层需按主异常判定语义；fallback
                # 失败仅作为 __cause__ 链上保留诊断信息，不覆盖主异常。
                assert last_exc is not None  # 走到 fallback 必然主调用已失败
                raise last_exc from fallback_exc

        assert last_exc is not None
        raise last_exc

    def _calculate_delay(
        self,
        attempt: int,
        retry_after: float | None = None,
    ) -> float:
        """计算退避延迟（指数 + 可选随机抖动，429 时尊重服务端 Retry-After）。

        Retry-After 封顶语义（工业级，对齐 OpenAI SDK 的「合理区间」判断）：
            合理区间 `0 < retry_after <= max_delay` 内 → 尊重服务端建议
                （可能比指数退避更长，如服务端要求 10s 就等 10s）；
            超出 max_delay（异常/恶意大值）→ 忽略并回退指数退避——
                指数退避本身已被 max_delay 封顶，单次最长等待有界，
                不会因 `retry-after: 3600` 挂死一小时。
        """
        delay = self.config.base_delay * (2**attempt)
        delay = min(delay, self.config.max_delay)
        if self.config.use_jitter:
            delay = random.uniform(0, delay)
        if retry_after is not None and 0 < retry_after <= self.config.max_delay:
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


# =====================================================================
# 重试执行器管理（按 model_key 跨请求共享）
# =====================================================================


class RetryHandlerManager:
    """
    按 model_key 提供共享 RetryHandler 实例（内含跨请求共享的 CircuitBreaker）。

    与 ClientManager / ReservationLimiterManager 同款缓存模式：同一 model_key 复用
    - 同一个 RetryHandler的熔断窗口（滑动窗口错误率）需跨请求积累，每次 new
    会清空窗口、等于熔断永不触发（create 阶段熔断失效的隐性缺陷根源）。
    - main / reasoning / fast 是不同模型/端点，独立熔断（reasoning 故障不熔断 fast）。
    - 配置（重试/熔断）由外层 register_config() 注入（Container 读 settings 后调用），
    子模块不再直接依赖 settings；修改配置后 reset() 重建实例生效；现在多实例共享同一份配置。
    """

    _instances: ClassVar[dict[str, RetryHandler]] = {}
    _config: ClassVar[RetryConfig | None] = None
    _circuit_breaker_config: ClassVar[CircuitBreakerConfig | None] = None

    @classmethod
    def register_config(
        cls,
        config: RetryConfig | None = None,
        circuit_breaker_config: CircuitBreakerConfig | None = None,
    ) -> None:
        """注入重试/熔断配置，并重建已缓存实例使新配置生效。

        Args:
            config: 重试配置（None 保持现有或默认）
            circuit_breaker_config: 熔断器配置（None 保持现有或默认）
        """
        if config is not None:
            cls._config = config
        if circuit_breaker_config is not None:
            cls._circuit_breaker_config = circuit_breaker_config
        cls.reset()

    @classmethod
    def get(cls, model_key: str = "main") -> RetryHandler:
        """获取指定 key 的 RetryHandler（懒创建 + 缓存复用）。

        model_key 由外部传入（与 ClientManager 对齐，不内置白名单）：
        任意 key 都懒构建——未配置过的 key 用注入的全局配置或默认值。

        Args:
            model_key: 模型标识（如 main / reasoning / fast / 自定义）

        Returns:
            共享 RetryHandler 实例（含共享 CircuitBreaker）
        """
        if model_key not in cls._instances:
            cls._instances[model_key] = cls._build()
        return cls._instances[model_key]

    @classmethod
    def reset(cls) -> None:
        """清空所有缓存实例（配置变更或测试时调用）。"""
        cls._instances.clear()

    @classmethod
    def _build(cls) -> RetryHandler:
        """按注入配置构建 RetryHandler；未 register_config 时用硬编码默认值。

        重试/熔断配置为进程级全局一致，不按 model_key 差异化。
        """
        return RetryHandler(
            config=cls._config or RetryConfig(),
            circuit_breaker=CircuitBreaker(
                config=cls._circuit_breaker_config or CircuitBreakerConfig(),
            ),
        )
