"""
ParameterValidator 单元测试

覆盖：
    jsonschema 完整校验：类型 / 必填 / 枚举 / 范围 / 未知参数拒绝
    全量错误收集（iter_errors，一次返回全部问题）
    format_issues 拼接格式
    validate_or_raise 异常语义
    BaseTool.validate_parameters 布尔委托 + validation_issues 字符串列表
"""

import copy
from typing import Any
from unittest.mock import patch

import pytest
from jsonschema import SchemaError

from app.domain.ports.tool_gateway import ErrorCode
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.validator import (
    ParameterValidationError,
    ParameterValidator,
)
from app.shared.json_schema import JSON_SCHEMA_DIALECT
from app.shared.json_schema import create_schema_validator
from tests.tool_lifecycle import StandaloneToolService as ToolService

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


_INVALID_SCHEMAS = [
    {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
    {"$schema": "urn:unsupported:dialect", "type": "object"},
    {"type": "invalid"},
    {"type": "object", "additionalProperties": 1},
    {"properties": {"unused": {"$schema": "http://json-schema.org/draft-07/schema#"}}},
]


class _SchemaTool(_NamedTool):
    """可配置 Schema 的工具，用真实执行计数验证前置拦截。"""

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema
        self.parameter_reads = 0
        self.executions = 0

    @property
    def parameters(self) -> dict[str, Any]:
        """记录读取次数，保证导出校验与载荷使用同一次快照。"""
        self.parameter_reads += 1
        return self.schema

    async def execute(self, **kwargs: Any) -> ToolResult:
        """记录实际工具副作用是否启动。"""
        self.executions += 1
        return ToolResult(success=True, content="ok")


@pytest.mark.parametrize("schema", _INVALID_SCHEMAS)
def test_invalid_schema_preserves_validation_interfaces(schema: dict[str, Any]) -> None:
    """非法定义返回清晰问题，布尔接口不抛出，异常接口保持原异常类型。"""
    validator = ParameterValidator()
    issues = validator.validate(schema, {})

    assert len(issues) == 1
    assert issues[0].message.startswith("Schema 定义无效:")
    assert _SchemaTool(schema).validate_parameters() is False
    with pytest.raises(ParameterValidationError, match="Schema 定义无效"):
        validator.validate_or_raise(schema, {})


def test_tool_parameter_dependencies_use_202012_without_mutation() -> None:
    """明确2020-12的字段依赖生效，校验不污染工具Schema。"""
    schema = {
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object",
        "properties": {"equipment": {"type": "string"}, "time": {"type": "string"}},
        "dependentRequired": {"equipment": ["time"]},
    }
    original = copy.deepcopy(schema)
    validator = ParameterValidator()

    assert validator.validate(schema, {"equipment": "EQ-01", "time": "now"}) == []
    issues = validator.validate(schema, {"equipment": "EQ-01"})
    assert len(issues) == 1
    assert "time" in issues[0].message
    assert schema == original


def test_recursive_reference_uses_effective_unknown_field_policy() -> None:
    """根递归引用也应用未知字段限制，不能回到未收紧的原 Schema。"""
    schema = {"type": "object", "properties": {"child": {"$ref": "#"}}}
    original = copy.deepcopy(schema)

    issues = ParameterValidator().validate(schema, {"child": {"extra": 1}})

    assert len(issues) == 1
    assert "extra" in issues[0].message
    assert schema == original


@pytest.mark.parametrize("method", ["to_openai_tool", "to_openai_response"])
@pytest.mark.parametrize("schema", _INVALID_SCHEMAS)
def test_tool_exports_reject_invalid_schema(method: str, schema: dict[str, Any]) -> None:
    """两种模型协议导出都拒绝非法定义，避免发送已知非法Schema。"""
    tool = _SchemaTool(schema)

    with pytest.raises(SchemaError):
        getattr(tool, method)()

    assert tool.parameter_reads == 1


@pytest.mark.parametrize("method", ["to_openai_tool", "to_openai_response"])
def test_tool_exports_validate_and_return_same_parameter_snapshot(method: str) -> None:
    """导出只读取一次parameters，避免校验对象与实际发送对象不同。"""
    schema = {"$schema": JSON_SCHEMA_DIALECT, "type": "object", "properties": {}}
    tool = _SchemaTool(schema)

    exported = getattr(tool, method)()
    parameters = exported["function"]["parameters"] if method == "to_openai_tool" else exported["parameters"]

    assert tool.parameter_reads == 1
    assert parameters is schema


@pytest.mark.asyncio
@pytest.mark.parametrize("schema", _INVALID_SCHEMAS)
async def test_invalid_schema_prevents_real_tool_execution(schema: dict[str, Any]) -> None:
    """注册后定义失效时执行链仍失败关闭：VALIDATION + 真实副作用为零。

    注册期守卫（见 test_tool_registry_metadata.py）已挡住常规入口；本用例覆盖
    定义在注册后变化的情形，保证执行链不因定义失效而放行或崩溃。
    """
    tool = _SchemaTool({"type": "object"})
    service = ToolService()
    service.register(tool)
    tool.schema = schema  # 注册后定义失效

    result = await service.execute(tool.name, {})

    assert result.success is False
    assert result.error_code == ErrorCode.VALIDATION
    assert "Schema 定义无效" in result.error
    assert tool.executions == 0


@pytest.mark.parametrize(
    "reject_unknown,additional,expected",
    [
        (False, None, 1),
        (False, True, 1),
        (True, None, 2),
        (True, False, 1),
        (True, True, 2),
        (True, {"type": "integer"}, 2),
    ],
)
def test_only_preflights_original_when_overriding_constraint(
    reject_unknown: bool,
    additional: Any,
    expected: int,
) -> None:
    """未修改未知字段策略时复用原校验器，修改后为有效根重新创建。"""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    if additional is not None:
        schema["additionalProperties"] = additional
    with patch("app.integration.tools.validator.create_schema_validator", wraps=create_schema_validator) as create:
        assert ParameterValidator(reject_unknown=reject_unknown).validate(schema, {"name": "x"}) == []
    assert create.call_count == expected


@pytest.mark.parametrize(
    "additional",
    [
        {"type": "invalid"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
        {"$ref": "#missing"},
    ],
)
def test_overridden_additional_schema_still_requires_preflight(additional: dict[str, Any]) -> None:
    """被收紧策略移除的子定义，其非法约束、方言和引用不能被覆盖掩盖。"""
    issues = ParameterValidator().validate({"additionalProperties": additional}, {})
    assert len(issues) == 1
    assert issues[0].message.startswith("Schema 定义无效:")


def test_reference_invalidated_by_wrapping_returns_definition_issue() -> None:
    """收紧策略使原本合法的指针失效时，仍走定义错误问题列表出口。"""
    schema = {
        "additionalProperties": {"$defs": {"value": {"type": "object"}}},
        "$ref": "#/additionalProperties/$defs/value",
    }
    assert create_schema_validator(schema).is_valid({})
    issues = ParameterValidator().validate(schema, {})
    assert len(issues) == 1
    assert issues[0].message.startswith("Schema 定义无效:")


@pytest.mark.parametrize("reference", ["#/additionalProperties", "#/%61dditionalProperties"])
def test_wrapping_cannot_repair_invalid_original_reference(reference: str) -> None:
    """新增未知字段约束不能意外修好原定义中未触发分支的悬空引用。"""
    schema = {"type": "object", "properties": {"unused": {"$ref": reference}}}
    issues = ParameterValidator().validate(schema, {})
    assert len(issues) == 1
    assert issues[0].message.startswith("Schema 定义无效:")
