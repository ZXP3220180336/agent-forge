"""
StreamParser — 流式/非流式响应解析

职责：
    1. 逐 chunk 解析流式响应，产出结构化结果
    2. 支持非流式完整响应解析
    3. 与 Agent 层的事件构造解耦（不依赖 events 模块）

从 llm_service._process_stream_response 提取并纯化。

用法：
    # 流式：逐 chunk 解析 → ParsedChunk
    chunk = StreamParser.parse_chunk(raw_chunk)
    if chunk.tool_call_deltas:
        tool_calls = StreamParser.merge_tool_calls(chunk.tool_call_deltas)

    # 非流式：一次解析完整响应 → dict
    result = StreamParser.parse_non_stream(response)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedChunk:
    """
    单个 chunk 的解析结果。

    - reasoning / message → 单 token
    - finish_reason / usage / refusal → 元数据
    """

    reasoning_token: str | None = None
    message_token: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    refusal: str | None = None
    tool_call_deltas: list[ToolCallDelta] | None = None


@dataclass
class ToolCallDelta:
    """工具调用的增量片段。"""

    index: int
    id: str = ""
    function_name: str = ""
    function_arguments: str = ""


class StreamParser:
    """
    流式响应解析器。

    纯数据层，不依赖 events 模块，不产出 SSE 字符串。
    由调用方决定如何将解析结果转换为事件。
    """

    @staticmethod
    def parse_chunk(chunk: Any) -> ParsedChunk:
        """
        解析单个 chunk，返回结构化结果。

        Args:
            chunk: OpenAI SDK 流式 chunk

        Returns:
            ParsedChunk 实例（各字段可能为空）
        """
        result = ParsedChunk()

        # usage：独立于 choices/delta 提取——usage-only chunk 的 choices 通常为空，
        # 但某些代理/适配层可能在带 delta 的 chunk 上也附带 usage，不应静默丢弃。
        if chunk.usage:
            result.usage = chunk.usage.model_dump()

        # finish_reason：提取独立于 delta 是否为空——finish chunk 的 delta 可能为 None
        # 或空对象，但 finish_reason 在 choices[0] 上，不能因 delta 为空而丢失。
        if chunk.choices and chunk.choices[0].finish_reason is not None:
            result.finish_reason = chunk.choices[0].finish_reason

        # 无内容增量（usage-only / finish-only chunk）→ 到此为止
        if not chunk.choices or not chunk.choices[0].delta:
            return result

        delta = chunk.choices[0].delta

        # reasoning_content
        if hasattr(delta, "reasoning_content") and delta.reasoning_content:
            result.reasoning_token = delta.reasoning_content

        # content
        if hasattr(delta, "content") and delta.content:
            result.message_token = delta.content

        # refusal（OpenAI 流式拒答形态，delta.refusal 到达）
        if hasattr(delta, "refusal") and delta.refusal:
            result.refusal = delta.refusal

        # tool_calls
        if hasattr(delta, "tool_calls") and delta.tool_calls:
            result.tool_call_deltas = []
            for tc in delta.tool_calls:
                tcd = ToolCallDelta(tc.index)
                if tc.id:
                    tcd.id = tc.id
                if tc.function:
                    if tc.function.name:
                        tcd.function_name = tc.function.name
                    if tc.function.arguments:
                        tcd.function_arguments = tc.function.arguments
                result.tool_call_deltas.append(tcd)

        return result

    @staticmethod
    def merge_tool_calls(
        deltas: list[ToolCallDelta],
    ) -> list[dict[str, Any]]:
        """
        将累积的 ToolCallDelta 列表合并为完整的 tool_calls 列表。

        Args:
            deltas: 所有 chunk 的 ToolCallDelta（已按 index 分组）

        Returns:
            OpenAI 格式的 tool_calls 列表
        """
        acc: dict[int, dict[str, Any]] = {}
        for d in deltas:
            if d.index not in acc:
                acc[d.index] = {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            if d.id:
                acc[d.index]["id"] = d.id
            if d.function_name:
                acc[d.index]["function"]["name"] += d.function_name
            if d.function_arguments:
                acc[d.index]["function"]["arguments"] += d.function_arguments

        indices = sorted(acc.keys())
        return [acc[i] for i in indices]

    @staticmethod
    def parse_non_stream(response: Any) -> dict[str, Any]:
        """
        解析非流式完整响应。

        Args:
            response: OpenAI SDK 非流式响应

        Returns:
            {
                "content": str,
                "finish_reason": str | None,
                "tool_calls": list[dict],
                "usage": dict | None,
                "refusal": str | None,
            }
        """
        # 空 choices 防护：某些适配层/异常响应可能返回空 choices（无生成内容），
        # 直接 response.choices[0] 会抛裸 IndexError。返回空结果让调用方按
        # 「业务无结果」处理，而非不可读的索引异常。
        if not response.choices:
            return {
                "content": "",
                "finish_reason": None,
                "tool_calls": [],
                "usage": response.usage.model_dump() if response.usage else None,
                "refusal": None,
            }

        choice = response.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )

        return {
            "content": msg.content or "",
            "finish_reason": choice.finish_reason,
            "tool_calls": tool_calls,
            "usage": response.usage.model_dump() if response.usage else None,
            # refusal 必须保留 None 与空串的区分：`or ""` 会把拒答的 None 抹成空串，
            # 下游无法判断「未拒答」与「拒答但文本为空」——直接透传原值。
            "refusal": getattr(msg, "refusal", None),
        }
