"""
RetryHandler / CircuitBreaker 单元测试

覆盖问题 2/4 的回归防护：
    问题 2：熔断触发（record_failure 返回 True）后，剩余重试应立即停止，
            不再对已确认故障的下游发无用请求。
    问题 4：半开探针只允许单次调用（不进入重试循环）；
            OPEN 状态下收到的成功（重试泄漏 / fallback 成功）不得关闭熔断器。
"""

import time

import pytest

from app.services.llm.retry import (
    CircuitBreaker,
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


# =====================================================================
# 问题 2：熔断触发后，剩余重试立即停止
# =====================================================================


@pytest.mark.asyncio
async def test_open_breaker_stops_remaining_retries():
    """熔断触发后不应再继续剩余重试。

    配置：failure_threshold=2, max_retries=5（本可尝试 6 次）。
    修复前：第 2 次失败已把计数器打到阈值 → OPEN，但仍继续第 3~6 次调用。
    修复后：第 2 次失败触发 OPEN → 立即 break，仅调用 2 次。
    """
    calls = [0]
    handler = RetryHandler(
        config=RetryConfig(max_retries=5),
        circuit_breaker=CircuitBreaker(failure_threshold=2),
    )

    with pytest.raises(TimeoutError):
        await handler.execute(_fail_always(TimeoutError("boom"), calls))

    assert calls[0] == 2, f"熔断触发后仍重试，实际调用 {calls[0]} 次"
    assert handler.circuit_breaker.state.value == "open"


@pytest.mark.asyncio
async def test_threshold_not_reached_keeps_retrying():
    """未达阈值时，重试次数不受影响（防止误伤正常重试）。"""
    handler = RetryHandler(
        config=RetryConfig(max_retries=2),
        circuit_breaker=CircuitBreaker(failure_threshold=10),
    )

    # 3 次尝试全部失败（threshold=10 远未触发）
    with pytest.raises(TimeoutError):
        await handler.execute(_fail_always(TimeoutError("boom")))

    assert handler.circuit_breaker.state.value == "closed"


# =====================================================================
# 问题 4：半开探针单次调用；OPEN 下成功不得关闭熔断器
# =====================================================================


def _force_half_open(cb: CircuitBreaker) -> None:
    """把熔断器推进到 HALF_OPEN 状态（OPEN 后等过 recovery_timeout）。"""
    cb.record_failure()  # threshold=1 → OPEN
    assert cb.state.value == "open"
    cb._last_failure_time = time.monotonic() - 1000  # 模拟恢复时间已过
    assert cb.allow_request() is True  # 触发 OPEN → HALF_OPEN，当前作为探针 #1
    assert cb.state.value == "half_open"


@pytest.mark.asyncio
async def test_half_open_probe_fails_without_retry():
    """半开探针失败后：不再重试，熔断器保持 OPEN。

    修复前：探针失败 → OPEN，但重试循环继续 → 第 2 次调用失败 → 仍 OPEN
            （更坏的情况见下一条：第 2 次调用若成功会误关熔断器）。
    修复后：探针单次调用，失败即确认未恢复 → OPEN，只调用 1 次。
    """
    calls = [0]
    cb = CircuitBreaker(failure_threshold=1, half_open_max_requests=3)
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
async def test_open_record_success_does_not_close_breaker():
    """OPEN 状态下的 record_success 必须 no-op，不得把熔断器误关为 CLOSED。

    修复前的 bug：探针失败 → record_failure 把 state 置 OPEN → 旧代码继续重试
    → 重试泄漏的成功 → record_success 在 OPEN 下走"重置为 CLOSED"兜底分支
    → 熔断器被误关，后续请求全部放行打向仍故障的下游。
    修复后：探针单次调用（无重试泄漏），且 OPEN 下 record_success 为 no-op，
    双重防护。此处直接验证后者。
    """
    cb = CircuitBreaker(failure_threshold=1, half_open_max_requests=3)
    cb.record_failure()  # → OPEN
    assert cb.state.value == "open"

    # 假设发生重试泄漏 / fallback 成功，调用了 record_success
    cb.record_success()
    assert cb.state.value == "open", (
        f"OPEN 下 record_success 不应关闭熔断器（当前 {cb.state.value}），"
        "成功可能来自重试泄漏 / fallback，不能证明主链路恢复"
    )
    assert cb.failure_count == 1


# =====================================================================
# 连带修复：熔断 OPEN 时 fallback 仍可用，但不关闭熔断器
# =====================================================================


@pytest.mark.asyncio
async def test_breaker_open_fallback_still_serves_but_keeps_breaker_open():
    """熔断 OPEN 时主调用被拒，但 fallback 仍可尝试（服务不中断）。

    fallback 是纯兜底：其成败完全不触碰熔断器（状态、计数、时间戳都不变），
    熔断器保持 OPEN，等待主链路探针验证。
    """
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()  # → OPEN
    assert cb.state.value == "open"
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
    assert cb.failure_count == count_before, "fallback 成功不得改变熔断计数"
    assert cb._last_failure_time == opened_at, "fallback 成功不得改变冷却计时"


@pytest.mark.asyncio
async def test_fallback_success_does_not_reset_breaker():
    """CLOSED 下 fallback 成功不清零熔断计数（备用链路成功 ≠ 主链路恢复）。

    修复前：主链路重试耗尽 → fallback 成功 → record_success 清零计数，
    主链路持续故障时熔断器永远不会打开（每次都被 fallback 救场清零）。
    修复后：熔断计数只反映主链路，fallback 成功不触碰它。
    """
    handler = RetryHandler(
        config=RetryConfig(max_retries=0),  # 不重试，1 次失败直接 fallback
        circuit_breaker=CircuitBreaker(failure_threshold=100),
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
    assert handler.circuit_breaker.state.value == "closed"  # 计数 1 未达阈值，不误开


@pytest.mark.asyncio
async def test_fallback_failure_does_not_count_toward_breaker():
    """CLOSED 下 fallback 失败不计入熔断计数（备用链路失败 ≠ 主链路故障）。

    修复前：fallback 失败额外 record_failure，把备用链路故障记到主链路熔断器。
    修复后：熔断计数只反映主链路，fallback 失败不额外累计。
    """
    handler = RetryHandler(
        config=RetryConfig(max_retries=0),
        circuit_breaker=CircuitBreaker(failure_threshold=100),
    )

    async def call_fn():
        raise TimeoutError("main down")

    async def fallback_fn():
        raise ConnectionError("backup also down")

    with pytest.raises(ConnectionError):
        await handler.execute(call_fn, fallback_fn=fallback_fn)

    assert handler.circuit_breaker.failure_count == 1, (
        "熔断计数应只计主链路 1 次失败，fallback 失败不得额外累计"
    )


@pytest.mark.asyncio
async def test_open_fallback_failure_does_not_touch_breaker():
    """熔断 OPEN 下 fallback 兜底失败不触碰熔断器。

    修复前：OPEN 下 fallback 被当作主链路传入 _single_attempt，失败会
    record_failure → _failure_count 累加。修复后：fallback 纯兜底，
    熔断器的状态、计数、时间戳完全不受影响。
    """
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()  # → OPEN
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
    assert cb.failure_count == count_before, "OPEN 下 fallback 失败不得累计熔断计数"
    assert cb._last_failure_time == opened_at, "OPEN 下 fallback 失败不得改变冷却计时"


# =====================================================================
# _last_failure_time 语义：只在熔断器进入 OPEN 时更新
# =====================================================================


def test_open_failures_do_not_extend_recovery_window():
    """熔断 OPEN 下的延续失败不得推后恢复探测窗口。

    _last_failure_time 只在熔断器进入 OPEN 时设置一次（冷却期起点）。
    OPEN 期间 fallback 等失败若反复刷新它，allow_request() 的
    now - _last_failure_time >= recovery_timeout 判定会永不满足，
    熔断器永远无法进入 HALF_OPEN —— 下游已恢复也无法被探测到。
    """
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=1000)
    cb.record_failure()  # CLOSED → OPEN
    assert cb.state.value == "open"
    opened_at = cb._last_failure_time

    # 熔断期内多次失败（fallback 兜底失败）
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()

    assert cb._last_failure_time == opened_at, (
        "OPEN 下失败不得改写 _last_failure_time（否则恢复探测被无限推迟）"
    )
    assert cb.failure_count == 1, (
        "OPEN 下失败不得累计 _failure_count（熔断期间计数应冻结，"
        f"实际 {cb.failure_count}）"
    )
    assert cb.state.value == "open"


def test_probe_failure_resets_recovery_clock():
    """探针失败（HALF_OPEN→OPEN）应更新 _last_failure_time，开始新一轮冷却。

    与上一测试对照：冷却计时重置只发生在状态切换进 OPEN 时，
    探针失败重新打开熔断器属于新一轮故障，必须重新计时。
    """
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=1000)
    cb.record_failure()  # → OPEN
    opened_at = cb._last_failure_time

    # 模拟恢复期已过 → HALF_OPEN，放行探针
    cb._last_failure_time = time.monotonic() - 2000
    assert cb.allow_request() is True
    assert cb.state.value == "half_open"

    # 探针失败 → 回 OPEN，冷却重新计时（新起点 > 旧起点）
    cb.record_failure()
    assert cb.state.value == "open"
    assert cb._last_failure_time > opened_at, (
        "探针失败进入 OPEN 应重置冷却计时"
    )
