"""
ParameterValidator 单元测试

覆盖：
    jsonschema 完整校验：类型 / 必填 / 枚举 / 范围 / 未知参数拒绝
    全量错误收集（iter_errors，一次返回全部问题）
    format_issues 拼接格式
    validate_or_raise 异常语义
    BaseTool.validate_parameters 布尔委托 + validation_issues 字符串列表
"""

import pytest

from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.validator import (
    ParameterValidationError,
    ParameterValidator,
)

# 带完整约束的测试 schema
_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0, "maximum": 150},
        "mode": {"type": "string", "enum": ["fast", "slow"]},
    },
    "required": ["name"],
}


def test_type_mismatch_reports_expected_and_actual():
    """传 str 给 integer 参数 → 归因信息含期望/实际类型。"""
    issues = ParameterValidator().validate(_SCHEMA, {"name": "x", "age": "30"})

    assert len(issues) == 1
    assert "参数 'age' 类型应为 integer，实际为 string" in issues[0].message


def test_required_missing_reports_field():
    """缺少必填参数 → 归因信息含字段名。"""
    issues = ParameterValidator().validate(_SCHEMA, {"age": 30})

    assert len(issues) == 1
    assert "缺少必填参数 'name'" in issues[0].message


def test_enum_violation_lists_options():
    """枚举越界 → 归因信息列出允许值与实际值。"""
    issues = ParameterValidator().validate(_SCHEMA, {"name": "x", "mode": "turbo"})

    assert len(issues) == 1
    assert "参数 'mode' 必须是 ['fast', 'slow'] 之一，实际为 'turbo'" in issues[0].message


def test_range_violation_falls_back_to_message():
    """范围越界（minimum）→ 兜底归因含字段名与错误消息。"""
    issues = ParameterValidator().validate(_SCHEMA, {"name": "x", "age": -1})

    assert len(issues) == 1
    assert issues[0].message.startswith("参数 'age'")
    assert "minimum" in issues[0].message


def test_unknown_parameter_rejected_by_default():
    """未知参数默认拒绝（additionalProperties: False 包装生效）。"""
    issues = ParameterValidator().validate(_SCHEMA, {"name": "x", "extra": 1})

    assert len(issues) == 1
    assert "参数 'extra' 不在 schema 允许范围内" in issues[0].message


def test_unknown_parameter_allowed_when_reject_unknown_false():
    """reject_unknown=False 时未知参数放行。"""
    validator = ParameterValidator(reject_unknown=False)
    issues = validator.validate(_SCHEMA, {"name": "x", "extra": 1})

    assert issues == []


def test_valid_parameters_return_empty():
    """全通过 → 空列表。"""
    issues = ParameterValidator().validate(_SCHEMA, {"name": "x", "age": 30, "mode": "fast"})

    assert issues == []


def test_collects_all_issues_at_once():
    """一次收集全部错误（多错并发返回，非只报首个）。"""
    issues = ParameterValidator().validate(_SCHEMA, {"age": "30", "mode": "turbo"})

    messages = [i.message for i in issues]
    # required(name 缺失) + type(age) + enum(mode) 三条
    assert len(issues) == 3
    assert any("缺少必填参数 'name'" in m for m in messages)
    assert any("类型应为 integer" in m for m in messages)
    assert any("必须是 ['fast', 'slow'] 之一" in m for m in messages)


def test_format_issues_semicolon_joined():
    """format_issues 用分号拼接全部问题。"""
    validator = ParameterValidator()
    issues = validator.validate(_SCHEMA, {"age": "30"})
    formatted = validator.format_issues(issues)

    assert "缺少必填参数 'name'" in formatted
    assert "类型应为 integer" in formatted
    assert "; " in formatted


def test_validate_or_raise_raises_parameter_error():
    """validate_or_raise 有错抛 ParameterValidationError（携带归因）。"""
    with pytest.raises(ParameterValidationError) as exc_info:
        ParameterValidator().validate_or_raise(_SCHEMA, {"age": 30})

    assert "缺少必填参数 'name'" in str(exc_info.value)


def test_validate_or_raise_passes_when_valid():
    """validate_or_raise 参数合法时不抛异常。"""
    ParameterValidator().validate_or_raise(_SCHEMA, {"name": "x"})


class _NamedTool(BaseTool):
    """测试工具：name 必填 string，age 可选 integer。"""

    @property
    def name(self) -> str:
        return "named"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="ok")


def test_base_tool_validate_parameters_delegates():
    """BaseTool.validate_parameters 委托 jsonschema 校验（布尔语义保持）。"""
    tool = _NamedTool()

    assert tool.validate_parameters(name="x") is True
    assert tool.validate_parameters(name="x", age=3) is True
    assert tool.validate_parameters(name="x", age="3") is False  # 类型错误
    assert tool.validate_parameters(age=3) is False  # 必填缺失


def test_base_tool_validation_issues_returns_chinese_list():
    """BaseTool.validation_issues 返回中文归因字符串列表。"""
    tool = _NamedTool()

    assert tool.validation_issues(name="x") == []
    issues = tool.validation_issues(age="3")
    assert len(issues) == 2  # name 缺失 + age 类型错误
    assert any("缺少必填参数 'name'" in i for i in issues)
    assert any("类型应为 integer" in i for i in issues)
