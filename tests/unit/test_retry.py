"""
RetryHandler / CircuitBreaker 单元测试

覆盖问题 1/2/4 的回归防护（问题 1：工业级熔断判定——滑动窗口错误率）：
    问题 1：熔断判定改为滑动窗口错误率（Hystrix 模型），计数粒度为请求级
    问题 2：熔断 OPEN 后拒绝主调用，不再对故障下游发请求
    问题 4：半开探针只允许单次调用；OPEN 状态下收到成功不得关闭熔断器

关键行为：
    - 窗口内总请求 ≥ request_volume_threshold 且错误率 ≥ error_threshold → OPEN
    - 或 窗口内全部失败且失败数 ≥ all_failed_min → OPEN（低流量纯失败保护）
    - 429（限流）不计入窗口，只退避（尊重 Retry-After）
    - fallback 与熔断器完全隔离
"""

import time

import pytest

from app.services.llm.retry import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    RetryConfig,
    RetryHandler,
)


def _fail_always(exc: Exception, counter: list[int] | None = None):
    """永远失败的 call_fn，记录调用次数。"""

    async def call_fn():
        if counter is not None:
            counter[0] += 1
        raise exc

    return call_fn


class _RateLimited(Exception):
    """模拟 429 限流异常（含 Retry-After 头）。"""

    status_code = 429
    headers: dict[str, str] = {}


def _quick_cb(**kwargs) -> CircuitBreaker:
    """快速触发熔断的测试熔断器：单次失败即可 OPEN。"""
    return CircuitBreaker(request_volume_threshold=1, **kwargs)


def _force_open(cb: CircuitBreaker) -> None:
    """触发 OPEN（依赖 request_volume_threshold=1）。"""
    cb.record_failure()  # 窗口 1 失败 → total=1≥1, 错误率 100% → OPEN
    assert cb.state.value == "open"


def _force_half_open(cb: CircuitBreaker) -> None:
    """把熔断器推进到 HALF_OPEN 状态（OPEN 后等过 recovery_timeout）。"""
    _force_open(cb)
    cb._last_failure_time = time.monotonic() - 1000  # 模拟恢复时间已过
    assert cb.allow_request() is True  # 触发 OPEN → HALF_OPEN，当前作为探针 #1
    assert cb.state.value == "half_open"


# =====================================================================
# 问题 1：滑动窗口 + 错误率熔断判定
# =====================================================================


def test_window_error_rate_opens_breaker():
    """主判据：窗口内总请求达标且错误率达标 → OPEN。"""
    cb = CircuitBreaker(
        window_seconds=1000,
        error_threshold=0.5,
        request_volume_threshold=5,
    )
    # 5 次请求中 4 次失败 → 错误率 80% ≥ 50%，total=5 ≥ 5 → OPEN
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state.value == "open"


def test_error_rate_below_threshold_keeps_closed():
    """错误率低于阈值 → 不熔断（有成功稀释，错误率回落）。"""
    cb = CircuitBreaker(
        window_seconds=1000,
        error_threshold=0.5,
        request_volume_threshold=4,
    )
    # 4 次请求中 1 次失败 → 错误率 25% < 50% → 不熔断
    cb.record_success()
    cb.record_success()
    cb.record_success()
    cb.record_failure()
    assert cb.state.value == "closed"


def test_window_volume_not_reached_keeps_closed():
    """窗口内请求量不足 → 不评估，保持 CLOSED（防低流量误判）。"""
    cb = CircuitBreaker(
        window_seconds=1000,
        error_threshold=0.5,
        request_volume_threshold=5,
        all_failed_min=10,  # 关闭纯失败保护，只测主判据
    )
    # 只有 3 次请求全失败：total=3 < 5 不达请求量门槛；failures=3 < 10 也不达纯失败保护
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state.value == "closed"


def test_all_failed_low_volume_opens_breaker():
    """低流量纯失败保护：请求量不足但全部失败且达最小样本量 → 熔断。"""
    cb = CircuitBreaker(
        window_seconds=1000,
        error_threshold=0.5,
        request_volume_threshold=100,  # 主判据永不满足
        all_failed_min=3,
    )
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()  # 全部失败且 ≥ 3 → OPEN
    assert cb.state.value == "open"


def test_window_expiry_prunes_old_failures():
    """滑动窗口：过期失败记录被清理，不再计入统计。"""
    cb = CircuitBreaker(window_seconds=0.01, error_threshold=0.5, request_volume_threshold=3)
    # 注入一条早已过期的失败记录
    cb._window.append((time.monotonic() - 100, False))
    assert cb.failure_count == 0, "过期失败应被清理"


@pytest.mark.asyncio
async def test_request_granularity_one_record_per_execute():
    """请求粒度：一次 execute 的多次重试失败只记 1 次窗口失败。

    问题 1 的核心：修复前每次 call_fn() 失败都累计熔断计数
    （threshold=5, max_retries=2 时约 2 次请求即熔断）。
    修复后：一个请求的多重重试合并为 1 次结果。
    """
    handler = RetryHandler(
        config=RetryConfig(max_retries=3),  # 4 次 call_fn
        circuit_breaker=CircuitBreaker(),   # 默认窗口，1 次失败不熔断
    )

    with pytest.raises(TimeoutError):
        await handler.execute(_fail_always(TimeoutError("boom")))

    assert handler.circuit_breaker.failure_count == 1, (
        "一次 execute 的多重失败应合并为 1 次窗口失败"
    )
    assert handler.circuit_breaker.state.value == "closed"  # 1 次失败不熔断


# =====================================================================
# 问题 2：熔断 OPEN 后拒绝主调用
# =====================================================================


@pytest.mark.asyncio
async def test_open_breaker_rejects_call_fn():
    """熔断 OPEN 后，后续请求不执行 call_fn（快速拒绝，不再对故障下游发请求）。"""
    calls = [0]
    cb = _quick_cb()
    _force_open(cb)

    handler = RetryHandler(config=RetryConfig(max_retries=5), circuit_breaker=cb)

    async def call_fn():
        calls[0] += 1
        return "ok"

    with pytest.raises(CircuitBreakerOpenError):
        await handler.execute(call_fn)

    assert calls[0] == 0, f"熔断 OPEN 时不应执行 call_fn，实际 {calls[0]} 次"


# =====================================================================
# 问题 4：半开探针单次调用；OPEN 下成功不得关闭熔断器
# =====================================================================


@pytest.mark.asyncio
async def test_half_open_probe_fails_without_retry():
    """半开探针失败后：不再重试，熔断器保持 OPEN。

    探针走单次调用路径（_probe_attempt），失败即确认未恢复 → OPEN。
    """
    calls = [0]
    cb = _quick_cb(half_open_max_requests=3)
    _force_half_open(cb)

    handler = RetryHandler(
        config=RetryConfig(max_retries=3),
        circuit_breaker=cb,
    )

    with pytest.raises(TimeoutError):
        await handler.execute(_fail_always(TimeoutError("probe failed"), calls))

    assert calls[0] == 1, f"探针不应重试，实际调用 {calls[0]} 次"
    assert cb.state.value == "open", "探针失败后熔断器应保持 OPEN"


@pytest.mark.asyncio
async def test_probe_rate_limited_keeps_half_open():
    """探针收到 429：不计入熔断状态机，熔断器保持 HALF_OPEN（限流≠未恢复）。"""
    cb = _quick_cb()
    _force_half_open(cb)

    handler = RetryHandler(circuit_breaker=cb)

    async def call_fn():
        raise _RateLimited()

    with pytest.raises(_RateLimited):
        await handler.execute(call_fn)

    assert cb.state.value == "half_open", "探针 429 不得把熔断器打回 OPEN"


def test_open_record_success_does_not_close_breaker():
    """OPEN 状态下的 record_success 必须 no-op，不得把熔断器误关为 CLOSED。

    成功可能来自重试泄漏 / fallback，不能证明主链路恢复；恢复只能由
    主链路探针验证。
    """
    cb = _quick_cb(half_open_max_requests=3)
    _force_open(cb)

    # 假设发生重试泄漏 / fallback 成功，调用了 record_success
    cb.record_success()
    assert cb.state.value == "open", (
        f"OPEN 下 record_success 不应关闭熔断器（当前 {cb.state.value}）"
    )
    assert cb.failure_count == 1


# =====================================================================
# 连带修复：熔断 OPEN 时 fallback 仍可用，但不关闭熔断器
# =====================================================================


@pytest.mark.asyncio
async def test_breaker_open_fallback_still_serves_but_keeps_breaker_open():
    """熔断 OPEN 时主调用被拒，但 fallback 仍可尝试（服务不中断）。

    fallback 是纯兜底：其成败完全不触碰熔断器（状态、窗口、时间戳都不变）。
    """
    cb = _quick_cb()
    _force_open(cb)
    count_before = cb.failure_count
    opened_at = cb._last_failure_time

    handler = RetryHandler(circuit_breaker=cb)

    async def call_fn():
        raise AssertionError("熔断期间不应调用主链路")

    async def fallback_fn():
        return "fallback-ok"

    result = await handler.execute(call_fn, fallback_fn=fallback_fn)

    assert result == "fallback-ok"
    assert cb.state.value == "open", "fallback 成功不得关闭熔断器"
    assert cb.failure_count == count_before, "fallback 成功不得改变熔断窗口"
    assert cb._last_failure_time == opened_at, "fallback 成功不得改变冷却计时"


@pytest.mark.asyncio
async def test_fallback_success_does_not_reset_breaker():
    """CLOSED 下 fallback 成功不清零熔断窗口（备用链路成功 ≠ 主链路恢复）。

    修复前：主链路重试耗尽 → fallback 成功 → record_success 清零计数，
    主链路持续故障时熔断器永远不会打开（每次都被 fallback 救场清零）。
    修复后：熔断窗口只反映主链路，fallback 成功不触碰它。
    """
    handler = RetryHandler(
        config=RetryConfig(max_retries=0),  # 不重试，1 次失败直接 fallback
        circuit_breaker=CircuitBreaker(),   # 默认窗口，1 次失败不熔断
    )

    async def call_fn():
        raise TimeoutError("main down")

    async def fallback_fn():
        return "fallback-ok"

    result = await handler.execute(call_fn, fallback_fn=fallback_fn)

    assert result == "fallback-ok"
    assert handler.circuit_breaker.failure_count == 1, (
        "fallback 成功不得清零主链路失败计数（否则主链路持续故障永不熔断）"
    )
    assert handler.circuit_breaker.state.value == "closed"  # 1 次失败未达熔断条件


@pytest.mark.asyncio
async def test_fallback_failure_does_not_count_toward_breaker():
    """CLOSED 下 fallback 失败不计入熔断窗口（备用链路失败 ≠ 主链路故障）。"""
    handler = RetryHandler(
        config=RetryConfig(max_retries=0),
        circuit_breaker=CircuitBreaker(),
    )

    async def call_fn():
        raise TimeoutError("main down")

    async def fallback_fn():
        raise ConnectionError("backup also down")

    with pytest.raises(ConnectionError):
        await handler.execute(call_fn, fallback_fn=fallback_fn)

    assert handler.circuit_breaker.failure_count == 1, (
        "熔断窗口应只计主链路 1 次失败，fallback 失败不得额外累计"
    )


@pytest.mark.asyncio
async def test_open_fallback_failure_does_not_touch_breaker():
    """熔断 OPEN 下 fallback 兜底失败不触碰熔断器。

    OPEN 下 fallback 走纯兜底，熔断器的状态、窗口、时间戳完全不受影响。
    """
    cb = _quick_cb()
    _force_open(cb)
    opened_at = cb._last_failure_time
    count_before = cb.failure_count

    handler = RetryHandler(circuit_breaker=cb)

    async def call_fn():
        raise AssertionError("熔断期间不应调用主链路")

    async def fallback_fn():
        raise ConnectionError("backup down too")

    with pytest.raises(ConnectionError):
        await handler.execute(call_fn, fallback_fn=fallback_fn)

    assert cb.state.value == "open"
    assert cb.failure_count == count_before, "OPEN 下 fallback 失败不得累计熔断窗口"
    assert cb._last_failure_time == opened_at, "OPEN 下 fallback 失败不得改变冷却计时"


# =====================================================================
# _last_failure_time 语义：只在熔断器进入 OPEN 时更新
# =====================================================================


def test_open_failures_do_not_extend_recovery_window():
    """熔断 OPEN 下的延续失败不得推后恢复探测窗口。

    _last_failure_time 只在熔断器进入 OPEN 时设置一次（冷却期起点）。
    OPEN 期间多次 record_failure（no-op）不改写它，窗口统计冻结——
    否则 allow_request() 的 now - _last_failure_time >= recovery_timeout
    判定被反复推迟，熔断器永远无法进入 HALF_OPEN。
    """
    cb = _quick_cb(recovery_timeout=1000)
    _force_open(cb)
    opened_at = cb._last_failure_time

    # 熔断期内多次失败（fallback 兜底失败等误调用）
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()

    assert cb._last_failure_time == opened_at, (
        "OPEN 下失败不得改写 _last_failure_time（否则恢复探测被无限推迟）"
    )
    assert cb.failure_count == 1, (
        "OPEN 下失败不得累计窗口失败数（熔断期间统计应冻结，"
        f"实际 {cb.failure_count}）"
    )
    assert cb.state.value == "open"


def test_probe_failure_resets_recovery_clock():
    """探针失败（HALF_OPEN→OPEN）应更新 _last_failure_time，开始新一轮冷却。

    与上一测试对照：冷却计时重置只发生在状态切换进 OPEN 时，
    探针失败重新打开熔断器属于新一轮故障，必须重新计时。
    """
    cb = _quick_cb(recovery_timeout=1000)
    cb.record_failure()  # → OPEN
    opened_at = cb._last_failure_time

    # 模拟恢复期已过 → HALF_OPEN，放行探针
    cb._last_failure_time = time.monotonic() - 2000
    assert cb.allow_request() is True
    assert cb.state.value == "half_open"

    # 探针失败 → 回 OPEN，冷却重新计时（新起点 > 旧起点）
    cb.record_failure()
    assert cb.state.value == "open"
    assert cb._last_failure_time > opened_at, "探针失败进入 OPEN 应重置冷却计时"


# =====================================================================
# 429：不计入熔断，只退避（尊重 Retry-After）
# =====================================================================


@pytest.mark.asyncio
async def test_rate_limited_not_counted_toward_breaker():
    """429 不计入熔断：单次限流失败（正常配置下会熔断）不触发熔断。"""
    handler = RetryHandler(
        config=RetryConfig(max_retries=0),  # 不重试，1 次失败
        circuit_breaker=CircuitBreaker(request_volume_threshold=1),  # 正常 1 次失败即熔断
    )

    async def call_fn():
        raise _RateLimited()

    with pytest.raises(_RateLimited):
        await handler.execute(call_fn)

    cb = handler.circuit_breaker
    assert cb.state.value == "closed", "429 不应触发熔断"
    assert cb.failure_count == 0, "429 不应计入窗口失败"


@pytest.mark.asyncio
async def test_retry_after_respected():
    """429 退避尊重服务端 Retry-After，且总延迟不低于指数退避。"""
    calls = [0]
    handler = RetryHandler(
        config=RetryConfig(max_retries=1, base_delay=0.01, use_jitter=False),
        circuit_breaker=CircuitBreaker(),
    )

    class _RateLimitedWithHeader(_RateLimited):
        headers = {"retry-after": "0.05"}

    async def call_fn():
        calls[0] += 1
        raise _RateLimitedWithHeader()

    start = time.monotonic()
    with pytest.raises(_RateLimitedWithHeader):
        await handler.execute(call_fn)
    elapsed = time.monotonic() - start

    assert calls[0] == 2, "429 应触发重试"
    assert elapsed >= 0.05, f"退避应至少等 Retry-After(0.05s)，实际 {elapsed:.3f}s"
    assert handler.circuit_breaker.state.value == "closed", "429 重试不应熔断"


# =====================================================================
# 混合失败：一次请求含限流 + 可重试失败时，按"是否出现过下游故障"记录
# =====================================================================


def _make_sequence_handler(sequence: list[Exception]) -> RetryHandler:
    """构造按序抛异常的 handler（max_retries=2，即 3 次尝试）。"""
    handler = RetryHandler(
        config=RetryConfig(max_retries=2, base_delay=0.01, use_jitter=False),
        circuit_breaker=CircuitBreaker(),  # 默认窗口，1 次失败不熔断
    )
    idx = [0]

    async def call_fn():
        exc = sequence[idx[0]]
        idx[0] += 1
        raise exc

    return handler, call_fn


@pytest.mark.asyncio
async def test_mixed_failures_429_then_timeout_counts_once():
    """429 → 429 → 超时（最后一次是可重试失败）→ 应计入 1 次失败。

    当前实现按最后一次异常判断，碰巧正确；此处作为正向回归。
    """
    handler, call_fn = _make_sequence_handler(
        [_RateLimited(), _RateLimited(), TimeoutError("boom")]
    )

    with pytest.raises(TimeoutError):
        await handler.execute(call_fn)

    assert handler.circuit_breaker.failure_count == 1, (
        "请求中出现过超时（下游故障），应计入 1 次失败"
    )


@pytest.mark.asyncio
async def test_mixed_failures_timeout_then_429_counts_once():
    """超时 → 429 → 429（最后一次是限流）→ 仍应计入 1 次失败。

    修复前的 bug：只看最后一次异常（429）→ 漏记。但中间出现过超时，
    说明本次请求确实触及了下游故障，必须计入熔断窗口。
    """
    handler, call_fn = _make_sequence_handler(
        [TimeoutError("boom"), _RateLimited(), _RateLimited()]
    )

    with pytest.raises(_RateLimited):
        await handler.execute(call_fn)

    assert handler.circuit_breaker.failure_count == 1, (
        "请求中出现过超时（下游故障），即使最后一次是 429 也应计入失败"
    )


@pytest.mark.asyncio
async def test_mixed_failures_all_rate_limited_not_counted():
    """429 → 429 → 429（纯限流）→ 不计入熔断窗口。

    限流是客户端自身限额，不代表下游故障；即使连续 3 次限流也不得熔断。
    """
    handler, call_fn = _make_sequence_handler(
        [_RateLimited(), _RateLimited(), _RateLimited()]
    )

    with pytest.raises(_RateLimited):
        await handler.execute(call_fn)

    cb = handler.circuit_breaker
    assert cb.failure_count == 0, "纯限流请求不得计入熔断窗口"
    assert cb.state.value == "closed"
