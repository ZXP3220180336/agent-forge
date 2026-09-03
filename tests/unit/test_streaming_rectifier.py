"""
StreamingRectifier 直接单元测试

直接测 StreamingRectifier.rectified_stream()（不经 LLMService 间接覆盖）：
    - 首 token 前中断 → 整流重试
    - 已产出 token 后中断 → 不整流（放弃）
    - cancel_event 置位 → 不整流（优雅终止）
    - 整流上限耗尽 → 放弃
    - 结算闭环：成功 settle / 中断 cancel

与 test_stream_rectify.py（经 LLMService 间接覆盖）互补，聚焦整流策略自身行为。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.integration.llm.streaming_rectifier import RectifierContext, StreamingRectifier
from app.domain.ports.llm_gateway import StreamResult


# =====================================================================
# chunk mock（参照 test_stream_rectify 既有模式）
# =====================================================================


def _content_chunk(text: str):
    """产出回复文本的 chunk（算"首 token"）。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=text, tool_calls=None
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt: int, completion: int):
    """携带 usage 的 chunk（无 choices，不算"首 token"）。"""
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            model_dump=lambda: {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
        ),
    )


class _FakeStream:
    """异步可迭代流：按序 yield chunks，可在指定位置抛异常模拟中断。"""

    def __init__(self, chunks, fail_at=None, exc=None):
        self._chunks = list(chunks)
        self._fail_at = fail_at
        # 默认可恢复异常（TimeoutError → RETRYABLE，能触发整流）
        self._exc = exc or TimeoutError("connection reset")
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._fail_at is not None and self._i >= self._fail_at:
            self._i += 1
            raise self._exc
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


class _FakeCircuitBreaker:
    """模拟熔断器：只记录 record_failure 调用。"""

    def __init__(self):
        self.failures = 0

    def record_failure(self):
        self.failures += 1


class _FakeRetry:
    """模拟 RetryHandler：execute 依次返回预置流，记录调用次数与熔断器。"""

    def __init__(self, streams):
        self._streams = list(streams)
        self.calls = 0
        self.circuit_breaker = _FakeCircuitBreaker()

    async def execute(self, call_fn, fallback_fn=None):
        self.calls += 1
        if not self._streams:
            return _FakeStream([])
        return self._streams.pop(0)


class _FakeReservation:
    """模拟 Reservation：settle/cancel 计数。"""

    def __init__(self):
        self.settled = False
        self.settle_calls = 0
        self.cancel_calls = 0

    async def settle(self, actual=None):
        self.settled = True
        self.settle_calls += 1

    async def cancel(self):
        self.settled = True
        self.cancel_calls += 1


def _run(streams, cancel_event=None, stream_max_retries=1):
    """构造并驱动 rectified_stream，返回 (events, result, retry, reservation)。"""
    result = StreamResult()
    reservation = _FakeReservation()
    active = {"res": reservation}
    context = RectifierContext(result, active, {})
    retry = _FakeRetry(streams)

    async def collect():
        events = []
        async for event in StreamingRectifier.rectified_stream(
            create_fn=lambda: _FakeStream([]),
            retry=retry,
            cancel_event=cancel_event,
            stream_max_retries=stream_max_retries,
            context=context,
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    return events, result, retry, reservation


# =====================================================================
# 首 token 前中断 → 整流重试
# =====================================================================


def test_rectifies_pre_first_token_interrupt():
    """首 token 前中断（usage chunk 后 fail）→ 整流重试 → 第 2 次成功。"""
    # 尝试 1：usage 后中断（usage 不算首 token，TimeouError → RETRYABLE）；尝试 2：正常产出
    streams = [
        _FakeStream([_usage_chunk(10, 0)], fail_at=1),
        _FakeStream([_content_chunk("你好"), _usage_chunk(10, 2)]),
    ]
    events, result, retry, reservation = _run(streams)

    assert retry.calls == 2, "首 token 前中断应整流重试 1 次"
    assert result.content == "你好", "第 2 次尝试应产出完整内容"
    assert reservation.settle_calls == 1, "成功路径应 settle"
    assert reservation.cancel_calls == 0, "成功不应 cancel"
    assert all("error" not in e for e in events), f"不应有 error 事件: {events}"


# =====================================================================
# 已产出 token 后中断 → 不整流
# =====================================================================


def test_no_rectify_after_token_emitted():
    """已产出 content 后中断 → 不整流（避免重复输出）。"""
    streams = [
        _FakeStream([_content_chunk("部分")], fail_at=1, exc=RuntimeError("reset")),
    ]
    events, result, retry, reservation = _run(streams)

    assert retry.calls == 1, "已产出 token 后中断不应整流"
    assert result.content == "部分", "已产出部分应保留"
    assert any("error" in e for e in events), "应产出 error 事件"
    assert reservation.settle_calls == 1, "请求已发出（create 成功）→ settle"


def test_no_rectify_when_interrupt_after_usage_chunk():
    """已产出 content 后接 usage-only chunk 再中断 → 不整流。

    回归：_apply_chunk 返回「单 chunk 是否产出」，若直接覆盖 emitted_any，
    中断前的 usage-only chunk 会把累积值冲成 False，误判「首 token 前」而整流，
    造成重复输出 + 双倍计费。
    """
    streams = [
        # 尝试 1：产出 content 后，usage-only chunk 后再中断
        # （usage 不算首 token，但 content 已置 emitted_any=True）
        _FakeStream(
            [_content_chunk("你好"), _usage_chunk(10, 2)], fail_at=2, exc=TimeoutError("reset")
        ),
        # 若误整流，尝试 2 会执行；断言 calls==1 即证明未整流
        _FakeStream([_content_chunk("重复")]),
    ]
    events, result, retry, reservation = _run(streams)

    assert retry.calls == 1, "已产出 token（即便最后一个 chunk 是 usage-only）不应整流"
    assert result.content == "你好", "已产出部分应保留"
    assert any("error" in e for e in events), "应产出 error 事件"


# =====================================================================
# cancel 不整流
# =====================================================================


def test_cancel_event_no_rectify():
    """cancel_event 置位 → 循环入口拦截，不发起请求（LLM-006，优雅终止）。

    修复前：cancel 置位仍执行 create（calls==1），迭代时才终止。
    修复后：循环顶部检查 cancel，create_fn 不调用（calls==0），不发费不占配额。
    """
    cancel_event = asyncio.Event()
    cancel_event.set()
    # 即使有 error 触发，cancel 优先终止
    streams = [
        _FakeStream([_content_chunk("x")], fail_at=0, exc=RuntimeError("reset")),
    ]
    events, result, retry, reservation = _run(streams, cancel_event=cancel_event)

    assert retry.calls == 0, "cancel 置位应在循环入口拦截，不发起请求"
    assert any("error" in e for e in events), "应产出 error 事件"


def test_cancel_during_iteration_with_retryable_exc_does_not_feed_breaker():
    """迭代中 cancel 置位 + 流抛 RETRYABLE → 放弃但不喂熔断器（LLM-011）。

    竞态：cancel 在循环顶部检查后、迭代阶段置位，流同时抛 RETRYABLE——
    修复前 `_should_rectify` 因 cancel 返回 False 后统一走放弃分支喂熔断
    （用户取消被计入熔断窗口）；修复后放弃分支先判取消，不喂熔断 + 取消事件。
    """
    cancel_event = asyncio.Event()

    class _CancelThenRaiseStream(_FakeStream):
        async def __anext__(self):
            cancel_event.set()  # 迭代中置位取消
            return await super().__anext__()  # 抛 TimeoutError（RETRYABLE）

    streams = [_CancelThenRaiseStream([], fail_at=0, exc=TimeoutError("reset"))]
    result = StreamResult()
    reservation = _FakeReservation()
    context = RectifierContext(result, {"res": reservation}, {})
    retry = _FakeRetry(streams)

    async def collect():
        events = []
        try:
            async for event in StreamingRectifier.rectified_stream(
                create_fn=lambda: _FakeStream([]),
                retry=retry,
                cancel_event=cancel_event,
                stream_max_retries=1,
                context=context,
            ):
                events.append(event)
        except asyncio.CancelledError:
            pass
        return events

    events = asyncio.run(collect())

    assert retry.circuit_breaker.failures == 0, "用户取消不得喂熔断器"
    assert any("用户取消了请求" in e for e in events), "应产出取消事件"
    assert result.error == "用户取消"


# =====================================================================
# 整流上限耗尽
# =====================================================================


def test_rectify_exhausts_max_retries():
    """连续中断达上限 → 放弃，熔断器 feeding。"""
    streams = [
        _FakeStream([_usage_chunk(10, 0)], fail_at=1),
        _FakeStream([_usage_chunk(10, 0)], fail_at=1),
        _FakeStream([_usage_chunk(10, 0)], fail_at=1),
    ]
    events, result, retry, reservation = _run(streams, stream_max_retries=2)

    assert retry.calls == 3, "stream_max_retries=2 → 3 次尝试后放弃"
    assert retry.circuit_breaker.failures == 1, "放弃时 RETRYABLE 中断应喂熔断器"
    assert any("error" in e for e in events), "应产出 error 事件"


def test_rectify_clears_refusal_from_dead_stream():
    """整流清理应复位 result.refusal——拒绝类死流不残留元数据。

    修复前：整流清理块复位 finish_reason/usage/tool_deltas 但漏了 refusal。
    refusal 不置 emitted_any（纯拒绝流可整流），成功尝试会残留上一死流的
    refusal 元数据（StreamResult.refusal 非空 → 下游误判为拒答）。
    修复后：整流清理一并 result.refusal = None。
    """
    # 尝试 1：refusal chunk（无 message token，emitted_any=False）+ 中断 → 可整流
    refusal_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None,
                    content=None,
                    tool_calls=None,
                    refusal="抱歉，无法处理",
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    streams = [
        _FakeStream([refusal_chunk], fail_at=1, exc=TimeoutError("reset")),
        _FakeStream([_content_chunk("你好"), _usage_chunk(10, 2)]),
    ]
    events, result, retry, reservation = _run(streams)

    assert retry.calls == 2, "refusal 死流首 token 前中断应整流"
    assert result.content == "你好", "第 2 次尝试应产出完整内容"
    assert result.refusal is None, (
        f"整流后不应残留死流 refusal，实际 {result.refusal!r}"
    )
    assert reservation.settle_calls == 1, "成功路径应 settle"


def test_rectify_clears_usage_finish_from_dead_stream():
    """整流清理应复位死流带出的 usage/finish_reason——成功流不被死流收尾元数据污染。

    direct 补测：间接层 test_usage_only_interrupt_rectifies（经 LLMService）已证
    「仅收尾元数据后中断整流、残留清空」，此处不经编排直接锚定整流器自身——死流中断
    已置 finish_reason/usage，整流成功后若清理缺失，result 残留死流收尾标记
    （下游误判已收尾）+ usage 记错计费。
    """
    # 尝试 1：带 finish_reason + usage 的死流（无 content，emitted_any=False）后中断
    # → 可整流；若整流不清 usage/finish_reason，成功流会残留尝试 1 的死流元数据
    finish_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=None, tool_calls=None
                ),
                finish_reason="stop",
            )
        ],
        usage=_usage_chunk(10, 0).usage,
    )
    streams = [
        _FakeStream([finish_chunk], fail_at=1, exc=TimeoutError("reset")),
        _FakeStream([_content_chunk("你好"), _usage_chunk(1, 2)]),
    ]
    events, result, retry, reservation = _run(streams)

    assert retry.calls == 2, "收尾元数据-only 死流首 token 前中断应整流"
    assert result.content == "你好"
    assert result.finish_reason is None, (
        f"整流后不应残留死流 finish_reason，实际 {result.finish_reason!r}"
    )
    assert result.usage == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }, f"死流 usage 残留应被清空，取尝试 2 的值，实际 {result.usage!r}"
    assert reservation.settle_calls == 1, "成功路径应 settle"


# =====================================================================
# 结算闭环
# =====================================================================


def test_settle_on_success():
    """正常读完 → settle（退 TPM 差），不 cancel。"""
    streams = [_FakeStream([_content_chunk("ok"), _usage_chunk(10, 2)])]
    events, result, retry, reservation = _run(streams)

    assert reservation.settle_calls == 1, "成功应 settle"
    assert reservation.cancel_calls == 0, "成功不应 cancel"
    assert result.content == "ok"


def test_cancel_on_hard_interrupt():
    """迭代被 CancelledError 中断 → finally 兜底 settle(None)（保留配额，非退 RPM）。

    LLM-003：硬取消时 create 已成功、请求已发出——「已发出的请求」是不可回滚的
    已提交副作用，settle(None) 保留配额（RPM 真实消耗不退）+ 标记终态；而非
    cancel() 全额退（会导致客户端 RPM 虚增 → 服务端 429 风暴）。
    """
    # CancelledError 不被 except Exception 捕获 → 走 finally 的结算兜底
    class _CancelStream(_FakeStream):
        async def __anext__(self):
            raise asyncio.CancelledError()

    streams = [_CancelStream([])]
    result = StreamResult()
    reservation = _FakeReservation()
    context = RectifierContext(result, {"res": reservation}, {})
    retry = _FakeRetry(streams)

    async def collect():
        events = []
        try:
            async for event in StreamingRectifier.rectified_stream(
                create_fn=lambda: _FakeStream([]),
                retry=retry,
                cancel_event=None,
                stream_max_retries=1,
                context=context,
            ):
                events.append(event)
        except asyncio.CancelledError:
            pass
        return events

    asyncio.run(collect())

    assert reservation.settle_calls == 1, "硬取消应由 finally 兜底 settle(None)"
    assert reservation.cancel_calls == 0, "请求已发出，不应 cancel 退 RPM"
    assert reservation.settled, "settle 后应标记终态"


class _CancelOnSettleReservation:
    """settle(actual) 中途抛 CancelledError、settle(None) 成功置终态的 Reservation。

    与真实 Reservation 行为一致：settle(actual) 退款中途取消保持未终态
    （reservation_limiter 的终态标记设计），供外层兜底 settle(None) 收尾。
    """

    def __init__(self):
        self.settled = False
        self.settle_calls = 0
        self.cancel_calls = 0

    async def settle(self, actual=None):
        self.settle_calls += 1
        if actual is not None:
            raise asyncio.CancelledError()  # 退款循环中途被取消，未到终态
        self.settled = True  # settle(None)：无退款循环，直接标记终态

    async def cancel(self):
        self.cancel_calls += 1
        self.settled = True


def test_settle_cancelled_midway_finally_settles_none():
    """settle(actual) 退款中途被取消 → 未终态 res 塞回 active → finally 兜底 settle(None)。

    修复前（LLM-002 前）：_settle_active 先 pop("res") 再 await settle()，settle 被
    取消时 res 已从 active 弹出，finally pop 到 None 无法续退 → 配额永久泄漏。
    LLM-003 修复后：finally 兜底 settle(None)（保留配额 + 标记终态，不泄漏；
    请求已发出，不 cancel 退 RPM）。
    """
    streams = [_FakeStream([_content_chunk("ok"), _usage_chunk(10, 2)])]
    result = StreamResult()
    reservation = _CancelOnSettleReservation()
    active = {"res": reservation}
    context = RectifierContext(result, active, {})
    retry = _FakeRetry(streams)

    async def collect():
        try:
            async for _ in StreamingRectifier.rectified_stream(
                create_fn=lambda: _FakeStream([]),
                retry=retry,
                cancel_event=None,
                stream_max_retries=1,
                context=context,
            ):
                pass
        except asyncio.CancelledError:
            pass

    asyncio.run(collect())

    assert reservation.settle_calls == 2, "settle(actual) 抛 + finally 兜底 settle(None) 共 2 次"
    assert reservation.cancel_calls == 0, "请求已发出，不应 cancel 退 RPM"
    assert reservation.settled, "settle(None) 收尾后应标记终态"
    assert "res" not in active, "finally 兜底后 active 不应残留未结算 reservation"


# =====================================================================
# 整流退避：RATE_LIMITED 中断提取 Retry-After（封顶到 max_delay）
# =====================================================================

_TEST_MAX_DELAY = 0.05
_TEST_BASE_DELAY = 0.01


class _RateLimited429(Exception):
    """模拟 429 限流异常（RATE_LIMITED，可整流 + 携带 Retry-After 头）。"""

    status_code = 429
    headers: dict[str, str] = {}


def _drive_rectify(streams, monkeypatch):
    """驱动整流并捕获 asyncio.sleep 时长（monkeypatch 不真实等待）。

    整流退避配置注入小值（base=0.01 / max=0.05 / 无抖动），避免测试真实等待。
    """
    result = StreamResult()
    reservation = _FakeReservation()
    context = RectifierContext(result, {"res": reservation}, {})
    retry = _FakeRetry(streams)

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    _base, _max, _jitter = (
        StreamingRectifier._base_delay,
        StreamingRectifier._max_delay,
        StreamingRectifier._use_jitter,
    )
    StreamingRectifier._base_delay = _TEST_BASE_DELAY
    StreamingRectifier._max_delay = _TEST_MAX_DELAY
    StreamingRectifier._use_jitter = False
    try:
        asyncio.run(_collect_events(StreamingRectifier, retry, context))
    finally:
        StreamingRectifier._base_delay = _base
        StreamingRectifier._max_delay = _max
        StreamingRectifier._use_jitter = _jitter
    return sleeps


async def _collect_events(cls, retry, context):
    async for _ in cls.rectified_stream(
        create_fn=lambda: _FakeStream([]),
        retry=retry,
        cancel_event=None,
        stream_max_retries=1,
        context=context,
    ):
        pass


def test_rectify_respects_retry_after_normal(monkeypatch):
    """429 中断整流时尊重合理 Retry-After（≤ max_delay 区间内）。

    修复前：整流退避不提取 Retry-After，只用指数退避——服务端建议被忽略
    （429 中断不等待服务端退避时间）。
    """
    class _Rl(_RateLimited429):
        headers = {"retry-after": "0.03"}  # 合理值（≤ max_delay=0.05）

    streams = [
        _FakeStream([], fail_at=0, exc=_Rl()),
        _FakeStream([_content_chunk("ok"), _usage_chunk(10, 2)]),
    ]
    sleeps = _drive_rectify(streams, monkeypatch)
    assert sleeps, "整流应退避"
    assert sleeps[0] >= 0.03, f"合理 Retry-After 应被尊重，实际 {sleeps[0]:.3f}s"


def test_rectify_retry_after_capped_by_max_delay(monkeypatch):
    """429 中断整流时 Retry-After 封顶到 max_delay，不无限等待。

    修复后：提取 Retry-After 但封顶——异常大值（3600s）忽略，回退指数退避。
    """
    class _Rl(_RateLimited429):
        headers = {"retry-after": "3600"}  # 异常大值（应被封顶忽略）

    streams = [
        _FakeStream([], fail_at=0, exc=_Rl()),
        _FakeStream([_content_chunk("ok"), _usage_chunk(10, 2)]),
    ]
    sleeps = _drive_rectify(streams, monkeypatch)
    assert sleeps, "整流应退避"
    assert sleeps[0] <= _TEST_MAX_DELAY, (
        f"Retry-After 超 max_delay 应封顶，实际 {sleeps[0]:.3f}s"
    )


# =====================================================================
# 首包/空闲双阈值看门狗（LLM-ADR-014）
# =====================================================================


class _DelayedChunkStream:
    """按需在指定 anext 前 sleep（模拟慢首包 / 慢后续 chunk）。"""

    def __init__(self, chunks, delay_at, delay):
        self._chunks = list(chunks)
        self._delay_at = delay_at
        self._delay = delay
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        if self._i == self._delay_at:
            await asyncio.sleep(self._delay)
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


def _tiny_watchdog():
    """首包/空闲阈值与退避缩到亚秒（测完恢复），避免拖慢测试。"""
    saved = (
        StreamingRectifier._first_token_timeout,
        StreamingRectifier._chunk_idle_timeout,
        StreamingRectifier._base_delay,
        StreamingRectifier._use_jitter,
    )
    StreamingRectifier._first_token_timeout = 0.05
    StreamingRectifier._chunk_idle_timeout = 0.05
    StreamingRectifier._base_delay = 0.0
    StreamingRectifier._use_jitter = False
    return saved


def _restore_watchdog(saved):
    (
        StreamingRectifier._first_token_timeout,
        StreamingRectifier._chunk_idle_timeout,
        StreamingRectifier._base_delay,
        StreamingRectifier._use_jitter,
    ) = saved


def test_first_token_timeout_rectifies_slow_first_chunk():
    """首包宽阈值：首 chunk 迟到超阈值 → TimeoutError（首 token 前）→ 整流重试。"""
    saved = _tiny_watchdog()
    try:
        streams = [
            _DelayedChunkStream([_content_chunk("你好")], delay_at=0, delay=0.2),
            _FakeStream([_content_chunk("好"), _usage_chunk(10, 2)]),
        ]
        events, result, retry, reservation = _run(streams)
        assert retry.calls == 2, "首包超时应整流重试"
        assert result.content == "好"
        assert reservation.settle_calls == 1
    finally:
        _restore_watchdog(saved)


def test_chunk_idle_timeout_abandons_after_first_token():
    """空闲窄阈值：已产出后下一 chunk 迟到超阈值 → 放弃（防死流挂起），error 非空。"""
    saved = _tiny_watchdog()
    try:
        streams = [
            _DelayedChunkStream(
                [_content_chunk("你好"), _usage_chunk(10, 2)], delay_at=1, delay=0.2
            )
        ]
        events, result, retry, reservation = _run(streams)
        assert retry.calls == 1, "已产出后空闲超时不应整流（防重复输出）"
        assert result.content == "你好", "部分产出保留在 result 中"
        assert result.error, "放弃应置失败信号（看门狗 TimeoutError 空串回退类型名，非空）"
        assert reservation.settle_calls == 1, "放弃路径应 settle"
    finally:
        _restore_watchdog(saved)


# =====================================================================
# 半流续接（LLM-ADR-015）：已产出 content 中断 → 带前缀续写
# =====================================================================


def _reasoning_chunk(text: str):
    """产出思考文本的 chunk（置 has_reasoning，算"首 token"，阻断续接）。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=text, content=None, tool_calls=None
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _tool_call_chunk():
    """携带工具调用增量的 chunk（算"首 token"，阻断续接——partial JSON 无法跨请求）。"""
    tc = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="search", arguments='{"query":"x"'),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=None, tool_calls=[tc]
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _run_continue(streams, continue_streams, continuation_max_retries=1):
    """驱动带续接的 rectified_stream，返回 (events, result, retry, reservation, prefixes)。

    continue_streams：续接 attempt 依次取出的对象（FakeStream 或 Exception）。
    退避配置依赖调用方用 _tiny_watchdog/_restore_watchdog 收窄。
    """
    result = StreamResult()
    reservation = _FakeReservation()
    active = {"res": reservation}
    context = RectifierContext(result, active, {})
    retry = _FakeRetry(list(streams))
    cont_pool = list(continue_streams)
    prefixes: list[str] = []

    async def cont_fn(prefix):
        prefixes.append(prefix)
        if not cont_pool:
            return _FakeStream([])
        item = cont_pool.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def collect():
        events = []
        async for ev in StreamingRectifier.rectified_stream(
            create_fn=lambda: _FakeStream([]),
            retry=retry,
            cancel_event=None,
            stream_max_retries=1,
            context=context,
            continue_fn=cont_fn,
            continuation_max_retries=continuation_max_retries,
        ):
            events.append(ev)
        return events

    events = asyncio.run(collect())
    return events, result, retry, reservation, prefixes


def test_continuation_resumes_after_content_interrupt():
    """已产出 content 中断（不整流）→ 带前缀续接成功：部分 + 续写无缝合并。"""
    saved = _tiny_watchdog()
    try:
        events, result, retry, reservation, prefixes = _run_continue(
            streams=[
                _FakeStream(
                    [_content_chunk("部分内容")], fail_at=1, exc=TimeoutError("reset")
                )
            ],
            continue_streams=[
                _FakeStream([_content_chunk("续写"), _usage_chunk(10, 3)])
            ],
        )
        assert retry.calls == 1, "已产出 content 后不应整流（从头重启）"
        assert prefixes == ["部分内容"], "续接应携带已产出 content 作前缀"
        assert result.content == "部分内容续写", "成功 = 部分 + 续写无缝合并"
        assert result.error is None, "续接成功不应置失败信号"
        assert not any("error" in e for e in events), f"不应有 error 事件: {events}"
        assert reservation.settle_calls == 1, "attempt0 中断（请求已发出）应 settle"
    finally:
        _restore_watchdog(saved)


def test_continuation_strips_seam_overlap():
    """续接流首部与已产 content 尾部重叠 → 接缝剥离，客户端不看到重复拼接。

    已产 "ABC"，续接首部 "BC"（重叠）→ 剥离不产出；"DEF" 才作为新内容产出。
    """
    saved = _tiny_watchdog()
    try:
        events, result, _, _, prefixes = _run_continue(
            streams=[
                _FakeStream(
                    [_content_chunk("ABC")], fail_at=1, exc=TimeoutError("reset")
                )
            ],
            continue_streams=[
                _FakeStream(
                    [
                        _content_chunk("BC"),
                        _content_chunk("DEF"),
                        _usage_chunk(10, 3),
                    ]
                )
            ],
        )
        assert prefixes == ["ABC"]
        assert result.content == "ABCDEF", f"接缝重叠应剥离重复，实际 {result.content!r}"
        assert result.error is None
        # message 事件只有 attempt0 的 ABC 与续写剥离后的 DEF（BC 重叠被剥离）
        import json

        def _msg_texts(evs):
            texts = []
            for ev in evs:
                obj = json.loads(ev[len("data: ") :].strip())
                if obj.get("type") == "message":
                    texts.append(obj.get("content", ""))
            return texts

        assert _msg_texts(events) == ["ABC", "DEF"]
    finally:
        _restore_watchdog(saved)


def test_continuation_create_failure_degrades_to_abandon():
    """续接 create 失败（如 provider 不支持 prefix）→ 退化放弃：保留部分 + 原中断原因。"""
    saved = _tiny_watchdog()
    try:
        events, result, retry, _, prefixes = _run_continue(
            streams=[
                _FakeStream(
                    [_content_chunk("部分")], fail_at=1, exc=TimeoutError("reset")
                )
            ],
            continue_streams=[RuntimeError("prefix not supported")],
        )
        assert prefixes == ["部分"], "应尝试续接一次（尽力而为）"
        assert result.content == "部分", "退化放弃应保留已产出 content"
        assert result.error == "reset", "失败信号用原中断原因（对用户更贴切）"
        assert any("流式响应中断" in e for e in events), "应产出放弃 error 事件"
        assert retry.circuit_breaker.failures == 1, (
            "原中断为 RETRYABLE → 与未续接的放弃一致，喂熔断"
        )
    finally:
        _restore_watchdog(saved)


def test_continuation_off_when_max_zero():
    """continuation_max_retries=0 → 续接不触发，维持既有放弃路径。"""
    saved = _tiny_watchdog()
    try:
        events, result, retry, _, prefixes = _run_continue(
            streams=[
                _FakeStream(
                    [_content_chunk("部分")], fail_at=1, exc=TimeoutError("reset")
                )
            ],
            continue_streams=[],
            continuation_max_retries=0,
        )
        assert prefixes == [], "max=0 不应发起续接"
        assert result.content == "部分"
        assert result.error, "维持放弃：部分保留 + 失败信号"
        assert retry.circuit_breaker.failures == 1
    finally:
        _restore_watchdog(saved)


def test_no_continuation_when_reasoning_only():
    """reasoning 半段中断（content 空）→ 不续接（reasoning 续写语义未验证，保守排除）。"""
    saved = _tiny_watchdog()
    try:
        events, result, retry, _, prefixes = _run_continue(
            streams=[
                _FakeStream(
                    [_reasoning_chunk("思考中")], fail_at=1, exc=TimeoutError("reset")
                )
            ],
            continue_streams=[_FakeStream([_content_chunk("不应被消费")])],
        )
        assert prefixes == [], "reasoning 流不应续接"
        assert result.reasoning_content == "思考中", "已产出 reasoning 保留"
        assert result.content == ""
        assert result.error, "维持放弃（失败信号）"
        assert any("error" in e for e in events)
    finally:
        _restore_watchdog(saved)


def test_no_continuation_when_tool_call_partial():
    """tool_call 半成品中断 → 不续接（partial JSON 无法跨请求续接，维持放弃）。"""
    saved = _tiny_watchdog()
    try:
        events, result, retry, _, prefixes = _run_continue(
            streams=[
                _FakeStream(
                    [_tool_call_chunk()], fail_at=1, exc=TimeoutError("reset")
                )
            ],
            continue_streams=[_FakeStream([_content_chunk("不应被消费")])],
        )
        assert prefixes == [], "tool 半成品不应续接"
        assert result.tool_calls == [], "放弃分支不合并 tool_calls（partial 丢弃）"
        assert result.error, "维持放弃（失败信号）"
        assert retry.circuit_breaker.failures == 1
    finally:
        _restore_watchdog(saved)


def test_continuation_reinterrupt_budget_then_succeeds():
    """续接再断（预算内）→ 带新前缀再续接成功；usage/成本同整流语义（末次成功流）。"""
    saved = _tiny_watchdog()
    try:
        events, result, _, _, prefixes = _run_continue(
            streams=[
                _FakeStream([_content_chunk("A")], fail_at=1, exc=TimeoutError("reset"))
            ],
            continue_streams=[
                # 续接 1：再产 B 后断（预算余 1）
                _FakeStream([_content_chunk("B")], fail_at=1, exc=TimeoutError("reset")),
                # 续接 2：产 C 成功
                _FakeStream([_content_chunk("C"), _usage_chunk(10, 3)]),
            ],
            continuation_max_retries=2,
        )
        assert prefixes == ["A", "AB"], "续接链应逐次携带增长后的前缀"
        assert result.content == "ABC"
        assert result.error is None
        assert result.usage == {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
        }, "续接成功取末次完成流 usage（断流 attempt 不计，LLM-039）"
        assert not any("error" in e for e in events)
    finally:
        _restore_watchdog(saved)


def test_continuation_reinterrupt_budget_exhausted_abandons():
    """续接再断且预算耗尽 → 放弃：保留两段内容 + 失败信号 + 喂熔断。"""
    saved = _tiny_watchdog()
    try:
        events, result, retry, _, prefixes = _run_continue(
            streams=[
                _FakeStream([_content_chunk("A")], fail_at=1, exc=TimeoutError("reset"))
            ],
            continue_streams=[
                _FakeStream([_content_chunk("B")], fail_at=1, exc=TimeoutError("reset"))
            ],
            continuation_max_retries=1,
        )
        assert prefixes == ["A"], "预算 1 → 仅一次续接"
        assert result.content == "AB", "两段部分内容保留"
        assert result.error, "预算耗尽应放弃（失败信号）"
        assert retry.circuit_breaker.failures == 1, "最终 RETRYABLE 放弃应喂熔断"
        assert any("error" in e for e in events)
    finally:
        _restore_watchdog(saved)
