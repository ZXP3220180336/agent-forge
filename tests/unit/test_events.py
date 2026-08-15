"""app/shared/events.py SSE 事件构建函数单元测试"""

from app.shared.events import (
    AgentEventType,
    build_done_event,
    build_error_event,
    build_info_event,
    build_message_event,
    build_reasoning_event,
    build_sse_event,
    build_tool_call_event,
    build_tool_result_event,
)


def test_agent_event_type_values():
    """7 种事件类型的枚举值"""
    assert AgentEventType.REASONING.value == "reasoning"
    assert AgentEventType.MESSAGE.value == "message"
    assert AgentEventType.TOOL_CALL.value == "tool_call"
    assert AgentEventType.TOOL_RESULT.value == "tool_result"
    assert AgentEventType.DONE.value == "done"
    assert AgentEventType.ERROR.value == "error"
    assert AgentEventType.INFO.value == "agent_info"


def test_agent_event_type_is_str_subclass():
    """AgentEventType 是 str 子类，可直接与字符串比较"""
    assert isinstance(AgentEventType.MESSAGE, str)
    assert AgentEventType.MESSAGE == "message"


def test_build_sse_event_string_type():
    """字符串类型原样进入 type 字段，输出 data: {json}\\n\\n"""
    assert build_sse_event("message", "hi") == 'data: {"type": "message", "content": "hi"}\n\n'


def test_build_sse_event_enum_type():
    """枚举类型取其 value"""
    assert build_sse_event(AgentEventType.MESSAGE, "hi") == (
        'data: {"type": "message", "content": "hi"}\n\n'
    )


def test_build_sse_event_extra_fields():
    """extra 字段平铺进 JSON 顶层"""
    assert build_sse_event("tool_call", "search", params={"q": "x"}, iteration=1) == (
        'data: {"type": "tool_call", "content": "search", "params": {"q": "x"}, "iteration": 1}\n\n'
    )


def test_build_sse_event_ensure_ascii_false():
    """中文不转义为 \\uXXXX"""
    raw = build_sse_event("message", "你好")
    assert "你好" in raw
    assert "\\u4f60" not in raw


def test_build_reasoning_event():
    assert build_reasoning_event("think") == 'data: {"type": "reasoning", "content": "think"}\n\n'


def test_build_message_event():
    assert build_message_event("hi") == 'data: {"type": "message", "content": "hi"}\n\n'


def test_build_tool_call_event():
    assert build_tool_call_event("search", {"q": "x"}, 1) == (
        'data: {"type": "tool_call", "content": "search", "params": {"q": "x"}, "iteration": 1}\n\n'
    )


def test_build_tool_result_event_rounds_duration():
    """duration 保留 3 位小数"""
    assert build_tool_result_event("web", "ok", 1.234567, 3) == (
        'data: {"type": "tool_result", "content": "ok", "tool": "web", "duration": 1.235, "iteration": 3}\n\n'
    )


def test_build_error_event():
    assert build_error_event("boom") == 'data: {"type": "error", "content": "boom"}\n\n'


def test_build_info_event():
    assert build_info_event("started") == 'data: {"type": "agent_info", "content": "started"}\n\n'


def test_build_done_event_default_tokens():
    """content 为空、total_tokens 默认 0"""
    assert build_done_event(3) == (
        'data: {"type": "done", "content": "", "iterations": 3, "total_tokens": 0}\n\n'
    )


def test_build_done_event_with_tokens():
    assert build_done_event(5, 120) == (
        'data: {"type": "done", "content": "", "iterations": 5, "total_tokens": 120}\n\n'
    )
