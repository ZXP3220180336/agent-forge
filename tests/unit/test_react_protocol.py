"""ReAct 纯工具协议转换测试。"""

import json

import pytest

from app.domain.reasoning._react_protocol import (
    _FINAL_ANSWER_TOOL,
    action_fingerprint,
    build_final_answer_tool,
    extract_final_answer,
    has_final_answer,
    tool_call_identity_error,
)


def _tool_call(call_id, name="echo", arguments="{}") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


_OUTPUT_SCHEMA = {"type": "object"}


def test_build_final_answer_tool_uses_schema_as_parameters():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }

    tool = build_final_answer_tool(schema)

    assert tool["function"]["name"] == _FINAL_ANSWER_TOOL
    assert tool["function"]["parameters"] is schema


def test_has_final_answer_detects_terminating_call_in_batch():
    other = _tool_call("1", "echo")

    assert has_final_answer([other, _tool_call("2", _FINAL_ANSWER_TOOL)], _OUTPUT_SCHEMA) is True


@pytest.mark.parametrize(
    "tool_calls",
    [
        [],
        [_tool_call("1", "echo")],
        [{"id": "2"}],  # 结构缺失的调用按非 final_answer 处理，不抛 KeyError
    ],
)
def test_has_final_answer_returns_false_for_other_batches(tool_calls):
    assert has_final_answer(tool_calls, _OUTPUT_SCHEMA) is False


def test_has_final_answer_ignores_same_named_unknown_tool_when_schema_disabled():
    """未启用 output_schema 时同名调用是模型臆造的未知工具，不能当终止信号。"""
    assert has_final_answer([_tool_call("1", _FINAL_ANSWER_TOOL)], None) is False


@pytest.mark.parametrize(
    ("tool_calls", "error_fragment"),
    [
        ([_tool_call(None)], "缺失"),
        ([_tool_call("   ")], "缺失"),
        ([_tool_call("same"), _tool_call("same")], "重复"),
    ],
)
def test_tool_call_identity_error_rejects_invalid_batch(tool_calls, error_fragment):
    assert error_fragment in tool_call_identity_error(tool_calls)


def test_tool_call_identity_error_accepts_unique_nonempty_ids():
    assert tool_call_identity_error([_tool_call("a"), _tool_call("b")]) is None


@pytest.mark.parametrize(
    ("arguments", "error_fragment"),
    [
        ("{", "JSON 解析失败"),
        ("[]", "JSON 对象"),
        (json.dumps({"answer": 1}), "不符合 schema"),
    ],
)
def test_extract_final_answer_rejects_invalid_arguments(arguments, error_fragment):
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    result, error = extract_final_answer(
        _tool_call("final", _FINAL_ANSWER_TOOL, arguments),
        schema,
    )

    assert result is None
    assert error_fragment in error


def test_extract_final_answer_returns_valid_object():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    expected = {"answer": "42"}

    result, error = extract_final_answer(
        _tool_call("final", _FINAL_ANSWER_TOOL, json.dumps(expected)),
        schema,
    )

    assert result == expected
    assert error is None


def test_action_fingerprint_normalizes_arguments_but_preserves_call_order():
    first = _tool_call("1", "alpha", '{"b": 2, "a": 1}')
    equivalent = _tool_call("2", "alpha", '{ "a": 1, "b": 2 }')
    second = _tool_call("3", "beta", "not-json")

    assert action_fingerprint([first]) == action_fingerprint([equivalent])
    assert action_fingerprint([first, second]) != action_fingerprint([second, first])
    assert "not-json" in action_fingerprint([second])


def test_action_fingerprint_keeps_final_answer_when_schema_mode_is_disabled():
    call = _tool_call(
        "final",
        _FINAL_ANSWER_TOOL,
        json.dumps({"answer": "ordinary tool call"}),
    )

    assert _FINAL_ANSWER_TOOL in action_fingerprint([call])
