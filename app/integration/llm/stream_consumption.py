"""单个 provider 流的读取、解析累积、接缝处理与关闭。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from app.platform.observability.logger import get_logger
from app.shared.events import build_message_event, build_reasoning_event
from app.shared.observation import isolate_observation

from .errors import _DeadlineExceeded, _StreamCancel
from .execution_control import _abort_trigger, _raise_if_aborted
from .streaming import StreamParser, ToolCallDelta

if TYPE_CHECKING:
    from app.domain.ports.llm_gateway import StreamResult

logger = get_logger("llm.stream_consumption")

_SEAM_OVERLAP_LIMIT = 64


class _SeamStripper:
    """剥离续接流首部与既有 content 尾部的有限重叠。"""

    def __init__(self, previous_content: str):
        self._tail = previous_content[-_SEAM_OVERLAP_LIMIT:] if previous_content else ""
        self._pending = ""
        self._done = not bool(self._tail)

    def push(self, token: str) -> str:
        """送入 content token，返回本次可以向调用方产出的文本。"""
        if self._done:
            return token
        self._pending += token
        overlap = _seam_overlap_len(self._tail, self._pending)
        if overlap < len(self._pending):
            self._done = True
            output = self._pending[overlap:]
            self._pending = ""
            return output
        if len(self._pending) >= _SEAM_OVERLAP_LIMIT:
            self._done = True
            output = self._pending
            self._pending = ""
            return output
        return ""

    def flush(self) -> None:
        """丢弃自然结束时仍完全命中既有尾部的缓冲文本。"""
        self._pending = ""
        self._done = True


def _seam_overlap_len(tail: str, text: str) -> int:
    """返回 tail 后缀与 text 前缀的最长重叠长度。"""
    limit = min(len(tail), len(text))
    for length in range(limit, -1, -1):
        if tail.endswith(text[:length]):
            return length
    return 0


async def _close_stream(response: Any) -> None:
    """尽力关闭未自然读完的 provider 流。"""
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — 清理失败不得覆盖原始终止或传输异常
        # 该告警在 except 块内、无保护：它抛错会替换正在展开的原始终止信号（G0-4）。
        # 先取出异常名再交给隔离边界：except 绑定名在块结束时被删除，不能进闭包。
        reason = type(exc).__name__
        isolate_observation(lambda: logger.warning("关闭 LLM 流失败: %s", reason))


def _apply_chunk(
    chunk: Any,
    result: StreamResult,
    tool_deltas: list[ToolCallDelta],
    *,
    seam: _SeamStripper | None = None,
) -> list[str]:
    """解析一个 chunk，累积结果并返回对应 SSE 事件。"""
    parsed = StreamParser.parse_chunk(chunk)
    events: list[str] = []

    if parsed.reasoning_token:
        result.reasoning_content += parsed.reasoning_token
        events.append(build_reasoning_event(parsed.reasoning_token))
    if parsed.has_reasoning:
        result.has_reasoning = True
    if parsed.message_token:
        text = parsed.message_token
        if seam is not None:
            text = seam.push(text)
        if text:
            result.content += text
            events.append(build_message_event(text))
    if parsed.finish_reason:
        result.finish_reason = parsed.finish_reason
    if parsed.refusal:
        result.refusal = parsed.refusal
    if parsed.tool_call_deltas:
        tool_deltas.extend(parsed.tool_call_deltas)
    if parsed.usage:
        result.usage = parsed.usage

    return events


async def drain_stream(
    response: Any,
    *,
    result: StreamResult,
    tool_deltas: list[ToolCallDelta],
    cancel_event: asyncio.Event | None,
    deadline: float | None,
    first_token_timeout: float,
    chunk_idle_timeout: float,
    seam_prefix: str | None = None,
) -> AsyncGenerator[str]:
    """读取单个流，累积结果并产出 SSE 事件。

    chunk、取消、期限与 idle 看门狗并发竞争；chunk 与终止同刻完成时先接管
    chunk 中的事实，再传播终止。提前退出时主动关闭流，正常 EOF 交给 SDK 收尾。

    Args:
        response: provider 流式响应。
        result: 当前调用链的累积结果。
        tool_deltas: 当前 attempt 的工具调用增量。
        cancel_event: 业务取消信号。
        deadline: monotonic 绝对期限。
        first_token_timeout: 首个 chunk 等待上限。
        chunk_idle_timeout: 后续 chunk 空闲上限。
        seam_prefix: 可选续接前缀；提供时剥离响应首部与此前缀尾部的重叠。

    Yields:
        当前 chunk 形成的 SSE 事件。
    """
    stream_iter = response.__aiter__()
    seam = _SeamStripper(seam_prefix) if seam_prefix is not None else None
    first_chunk = True
    stream_exhausted = False
    try:
        while True:
            idle = first_token_timeout if first_chunk else chunk_idle_timeout
            anext_task = asyncio.ensure_future(anext(stream_iter))
            abort_task = asyncio.ensure_future(_abort_trigger(cancel_event, deadline))
            try:
                done, _ = await asyncio.wait(
                    {anext_task, abort_task},
                    timeout=idle,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if anext_task in done:
                    try:
                        chunk = anext_task.result()
                    except StopAsyncIteration:
                        stream_exhausted = True
                        _raise_if_aborted(cancel_event, deadline)
                        break
                    first_chunk = False
                    events = _apply_chunk(
                        chunk,
                        result,
                        tool_deltas,
                        seam=seam,
                    )
                    _raise_if_aborted(cancel_event, deadline)
                elif abort_task in done:
                    reason = abort_task.result()
                    if reason == "cancel":
                        raise _StreamCancel()
                    raise _DeadlineExceeded()
                else:
                    raise TimeoutError("流式读取空闲超时" if not first_chunk else "流式读取首包超时")
            finally:
                for task in (anext_task, abort_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    anext_task,
                    abort_task,
                    return_exceptions=True,
                )

            for event in events:
                yield event
        if seam is not None:
            seam.flush()
    finally:
        if not stream_exhausted:
            await _close_stream(response)
