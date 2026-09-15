"""单流消费组件的事实接管、终止优先级与资源关闭测试。"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.ports.llm_gateway import StreamResult
from app.integration.llm.errors import _DeadlineExceeded, _StreamCancel
from app.integration.llm.stream_consumption import drain_stream


def _content_chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None,
                    content=text,
                    tool_calls=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt: int, completion: int) -> SimpleNamespace:
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


class _Stream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)
        self.close_calls = 0

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self) -> None:
        self.close_calls += 1


class _HangingStream(_Stream):
    async def __anext__(self) -> Any:
        await asyncio.Future()


async def _collect(
    stream: Any,
    result: StreamResult,
    **kwargs: Any,
) -> list[str]:
    events = []
    async for event in drain_stream(
        stream,
        result=result,
        tool_deltas=[],
        cancel_event=kwargs.get("cancel_event"),
        deadline=kwargs.get("deadline"),
        first_token_timeout=kwargs.get("first_token_timeout", 1.0),
        chunk_idle_timeout=kwargs.get("chunk_idle_timeout", 1.0),
        seam_prefix=kwargs.get("seam_prefix"),
    ):
        events.append(event)
    return events


async def test_completed_chunk_is_absorbed_before_deadline_and_stream_is_closed():
    """chunk 与期限同刻就绪时保留 usage，但不继续产出业务事件。"""
    stream = _Stream([_usage_chunk(7, 3)])
    result = StreamResult()

    with pytest.raises(_DeadlineExceeded):
        await _collect(stream, result, deadline=time.monotonic() - 1)

    assert result.usage == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }
    assert stream.close_calls == 1


async def test_cancel_wins_over_deadline_after_completed_chunk():
    """两种终止同时命中时仍先接管 chunk，并保持既有 cancel 优先级。"""
    cancel_event = asyncio.Event()
    cancel_event.set()
    stream = _Stream([_content_chunk("已接管")])
    result = StreamResult()

    with pytest.raises(_StreamCancel):
        await _collect(
            stream,
            result,
            cancel_event=cancel_event,
            deadline=time.monotonic() - 1,
        )

    assert result.content == "已接管"
    assert stream.close_calls == 1


async def test_eof_then_cancel_preserves_terminal_priority_without_extra_close():
    """EOF 与取消同刻命中时传播取消；自然耗尽的流无需重复关闭。"""
    cancel_event = asyncio.Event()
    cancel_event.set()
    stream = _Stream([])

    with pytest.raises(_StreamCancel):
        await _collect(stream, StreamResult(), cancel_event=cancel_event)

    assert stream.close_calls == 0


async def test_idle_timeout_closes_hanging_stream():
    stream = _HangingStream([])

    with pytest.raises(TimeoutError, match="首包超时"):
        await _collect(
            stream,
            StreamResult(),
            first_token_timeout=0.01,
        )

    assert stream.close_calls == 1


async def test_consumer_aclose_closes_stream_without_consuming_more_chunks():
    stream = _Stream([_content_chunk("A"), _content_chunk("B")])
    result = StreamResult()
    consumer = drain_stream(
        stream,
        result=result,
        tool_deltas=[],
        cancel_event=None,
        deadline=None,
        first_token_timeout=1.0,
        chunk_idle_timeout=1.0,
    )

    await anext(consumer)
    await consumer.aclose()

    assert result.content == "A"
    assert stream.close_calls == 1


async def test_seam_overlap_is_stripped_across_chunks():
    stream = _Stream([_content_chunk("world"), _content_chunk("!")])
    result = StreamResult()
    result.content = "hello world"

    events = await _collect(stream, result, seam_prefix=result.content)

    assert result.content == "hello world!"
    assert len(events) == 1
    assert stream.close_calls == 0
