"""
StreamParser 单元测试

覆盖流式/非流式响应解析：
    parse_chunk      各字段提取（reasoning / message / finish_reason / usage / tool_calls）
    merge_tool_calls 增量累积与合并（多工具交错 / 缺 id / 空列表）
    parse_non_stream 非流式完整响应解析

mock chunk 用 SimpleNamespace 模拟 OpenAI SDK 流式 chunk 形态，不走真实 API。
"""

from types import SimpleNamespace

from app.services.llm.streaming import StreamParser, ToolCallDelta


# =====================================================================
# mock chunk 构造
# =====================================================================


def _content_chunk(text: str):
    """回复文本 chunk。"""
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


def _reasoning_chunk(text: str):
    """推理文本 chunk。"""
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


def _finish_chunk(reason: str):
    """携带 finish_reason 的 chunk。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=None, tool_calls=None
                ),
                finish_reason=reason,
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt: int, completion: int):
    """携带 usage 的 chunk（无 choices）。"""
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


def _tool_call_chunk(index: int, id_: str = "", name: str = "", arguments: str = ""):
    """携带工具调用增量的 chunk。"""
    fn = None
    if name or arguments:
        fn = SimpleNamespace(name=name, arguments=arguments)
    tc = SimpleNamespace(index=index, id=id_, function=fn)
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


def _empty_chunk():
    """无内容增量、无 finish_reason 的 chunk。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=None, tool_calls=None
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


# =====================================================================
# parse_chunk — 各字段提取
# =====================================================================


def test_parse_content_chunk():
    """content chunk → message_token。"""
    p = StreamParser.parse_chunk(_content_chunk("你好"))
    assert p.message_token == "你好"
    assert p.reasoning_token is None
    assert p.finish_reason is None
    assert p.usage is None
    assert p.tool_call_deltas is None


def test_parse_reasoning_chunk():
    """reasoning chunk → reasoning_token。"""
    p = StreamParser.parse_chunk(_reasoning_chunk("思考中"))
    assert p.reasoning_token == "思考中"
    assert p.message_token is None


def test_parse_finish_reason_chunk():
    """finish_reason chunk → finish_reason，无内容。"""
    p = StreamParser.parse_chunk(_finish_chunk("tool_calls"))
    assert p.finish_reason == "tool_calls"
    assert p.message_token is None


def test_parse_usage_chunk():
    """usage chunk（无 choices）→ usage dict。"""
    p = StreamParser.parse_chunk(_usage_chunk(10, 5))
    assert p.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert p.message_token is None


def test_parse_tool_call_chunk():
    """tool_call chunk → ToolCallDelta。"""
    p = StreamParser.parse_chunk(
        _tool_call_chunk(0, id_="call_1", name="search", arguments='{"query":"x"}')
    )
    assert p.tool_call_deltas is not None
    d = p.tool_call_deltas[0]
    assert isinstance(d, ToolCallDelta)
    assert d.index == 0
    assert d.id == "call_1"
    assert d.function_name == "search"
    assert d.function_arguments == '{"query":"x"}'


def test_parse_tool_call_missing_fields():
    """tool_call 片段缺 id / function → 各字段兜底为空。"""
    p = StreamParser.parse_chunk(_tool_call_chunk(1))
    d = p.tool_call_deltas[0]
    assert d.index == 1
    assert d.id == ""  # 缺 id 兜底
    assert d.function_name == ""
    assert d.function_arguments == ""


def test_parse_empty_chunk():
    """空增量 chunk → 全空 ParsedChunk。"""
    p = StreamParser.parse_chunk(_empty_chunk())
    assert p.message_token is None
    assert p.reasoning_token is None
    assert p.finish_reason is None
    assert p.usage is None
    assert p.tool_call_deltas is None


# =====================================================================
# merge_tool_calls — 增量累积与合并
# =====================================================================


def test_merge_single_tool_call():
    """单个工具调用的参数增量跨 chunk 拼接。"""
    deltas = [
        ToolCallDelta(0, id="call_1", function_name="sea", function_arguments='{"q'),
        ToolCallDelta(0, function_name="rch", function_arguments='uery":"x"}'),
    ]
    result = StreamParser.merge_tool_calls(deltas)
    assert len(result) == 1
    assert result[0]["id"] == "call_1"
    assert result[0]["type"] == "function"
    assert result[0]["function"]["name"] == "search"
    assert result[0]["function"]["arguments"] == '{"query":"x"}'


def test_merge_multiple_tool_calls_interleaved():
    """多工具调用交错到达，按 index 分组正确合并。"""
    deltas = [
        ToolCallDelta(0, id="call_0", function_name="a", function_arguments='{"x'),
        ToolCallDelta(1, id="call_1", function_name="b", function_arguments='{"y'),
        ToolCallDelta(0, function_name="a", function_arguments='":1}'),
        ToolCallDelta(1, function_name="b", function_arguments='":2}'),
    ]
    result = StreamParser.merge_tool_calls(deltas)
    assert len(result) == 2
    assert result[0]["id"] == "call_0"
    assert result[0]["function"]["name"] == "aa"
    assert result[0]["function"]["arguments"] == '{"x":1}'
    assert result[1]["id"] == "call_1"
    assert result[1]["function"]["name"] == "bb"
    assert result[1]["function"]["arguments"] == '{"y":2}'


def test_merge_output_sorted_by_index():
    """输出按 index 排序（即使增量乱序到达）。"""
    deltas = [
        ToolCallDelta(2, id="c2", function_name="z", function_arguments="{}"),
        ToolCallDelta(0, id="c0", function_name="x", function_arguments="{}"),
        ToolCallDelta(1, id="c1", function_name="y", function_arguments="{}"),
    ]
    result = StreamParser.merge_tool_calls(deltas)
    assert [r["id"] for r in result] == ["c0", "c1", "c2"]


def test_merge_missing_id_fallback_by_index():
    """缺 id 时按 index 兜底（id 为空字符串），仍正确合并。"""
    deltas = [
        ToolCallDelta(0, function_name="a", function_arguments="{}"),
        ToolCallDelta(1, function_name="b", function_arguments="{}"),
    ]
    result = StreamParser.merge_tool_calls(deltas)
    assert len(result) == 2
    assert result[0]["id"] == ""
    assert result[0]["function"]["name"] == "a"


def test_merge_empty_list():
    """空增量列表 → 空列表。"""
    assert StreamParser.merge_tool_calls([]) == []


# =====================================================================
# parse_non_stream — 非流式完整响应
# =====================================================================


def _non_stream_response(content="", finish_reason="stop", tool_calls=None, usage=None):
    """构造非流式响应 mock。"""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_parse_non_stream_content():
    """非流式：提取 content / finish_reason。"""
    resp = _non_stream_response(content="你好", finish_reason="stop")
    r = StreamParser.parse_non_stream(resp)
    assert r["content"] == "你好"
    assert r["finish_reason"] == "stop"
    assert r["tool_calls"] == []
    assert r["usage"] is None


def test_parse_non_stream_tool_calls():
    """非流式：提取 tool_calls。"""
    tc = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="search", arguments='{"q":"x"}'),
    )
    resp = _non_stream_response(tool_calls=[tc])
    r = StreamParser.parse_non_stream(resp)
    assert r["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q":"x"}'},
        }
    ]


def test_parse_non_stream_usage():
    """非流式：提取 usage。"""
    usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 42})
    resp = _non_stream_response(usage=usage)
    r = StreamParser.parse_non_stream(resp)
    assert r["usage"] == {"total_tokens": 42}


def test_parse_non_stream_content_none():
    """非流式：content 为 None → 兜底为空字符串。"""
    resp = _non_stream_response(content=None)
    r = StreamParser.parse_non_stream(resp)
    assert r["content"] == ""


def test_parse_non_stream_empty_choices():
    """空 choices（适配层/异常响应）→ 返回空结果而非抛 IndexError（第 10 条修复）。"""
    resp = SimpleNamespace(choices=[], usage=None)
    r = StreamParser.parse_non_stream(resp)
    assert r == {
        "content": "",
        "finish_reason": None,
        "tool_calls": [],
        "usage": None,
        "refusal": None,
    }, "空 choices 应返回空结果，让调用方按业务无结果处理"


# =====================================================================
# 漏洞回归：finish_reason 独立于 delta / usage 共存 / 混合 chunk
# =====================================================================


def test_parse_finish_chunk_with_none_delta():
    """回归：delta 为 None 但带 finish_reason 的 chunk，finish_reason 不丢失。

    修复前：`not chunk.choices[0].delta` 守卫提前 return，丢失 finish_reason。
    真实 SDK 的 finish chunk 可能出现 delta=None 形态。
    """
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=None, finish_reason="length")],
        usage=None,
    )
    p = StreamParser.parse_chunk(chunk)
    assert p.finish_reason == "length"
    assert p.message_token is None
    assert p.usage is None


def test_parse_usage_with_empty_delta():
    """回归：带空 delta 与 usage 共存的 chunk，usage 不静默丢弃。

    修复前：`not chunk.choices[0].delta` 守卫在空 delta 时提前 return，usage 只在
    choices 为空时提取——代理/适配层违规在带 delta 的 chunk 上附 usage 时丢失。
    """
    usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 5})
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")],
        usage=usage,
    )
    p = StreamParser.parse_chunk(chunk)
    assert p.usage == {"total_tokens": 5}
    assert p.finish_reason == "stop"


def test_parse_mixed_content_and_tool_calls():
    """混合 chunk：同一 chunk 同时含 content 与 tool_calls，各自独立提取。"""
    tc = SimpleNamespace(
        index=0, id="call_1", function=SimpleNamespace(name="search", arguments='{"q":"x"}')
    )
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content="回复", tool_calls=[tc]
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    p = StreamParser.parse_chunk(chunk)
    assert p.message_token == "回复"
    assert p.tool_call_deltas is not None
    assert p.tool_call_deltas[0].function_name == "search"


def test_merge_id_override_preserves_first_non_empty():
    """merge：id 覆盖策略——首个非空 id 保留，后续空 id 不覆盖。"""
    deltas = [
        ToolCallDelta(0, id="call_1", function_name="a", function_arguments="{}"),
        ToolCallDelta(0, function_arguments="more"),  # 无 id，不覆盖
    ]
    result = StreamParser.merge_tool_calls(deltas)
    assert result[0]["id"] == "call_1"
    assert result[0]["function"]["arguments"] == "{}more"
