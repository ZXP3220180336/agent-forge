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

from app.services.llm.streaming_rectifier import RectifierContext, StreamingRectifier
from app.services.llm_service import StreamResult


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
    """cancel_event 置位 → 中断即返回，不整流（优雅终止）。"""
    cancel_event = asyncio.Event()
    cancel_event.set()
    # 即使有 error 触发，cancel 优先终止
    streams = [
        _FakeStream([_content_chunk("x")], fail_at=0, exc=RuntimeError("reset")),
    ]
    events, result, retry, reservation = _run(streams, cancel_event=cancel_event)

    assert retry.calls == 1, "cancel 置位不应整流"
    assert any("error" in e for e in events), "应产出 error 事件"


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
    """迭代被 CancelledError 中断 → finally 兜底 cancel（reservation 不泄漏）。"""
    # CancelledError 不被 except Exception 捕获 → 走 finally 的 cancel 兜底
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

    assert reservation.cancel_calls == 1, "硬取消应由 finally 兜底 cancel"
    assert reservation.settled, "cancel 后应标记终态"
