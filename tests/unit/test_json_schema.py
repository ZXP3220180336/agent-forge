"""共享 JSON Schema 契约：固定方言、真实约束与本地引用边界。"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator, SchemaError

from app.domain.reasoning.planner import PLAN_SCHEMA, PLAN_STEP_SCHEMA, REPLAN_SCHEMA, RESULT_SCHEMA
from app.domain.reasoning.reflection import CRITIQUE_SCHEMA, REFLECTION_SCHEMA
from app.shared.json_schema import JSON_SCHEMA_DIALECT, create_schema_validator

_OLD_DIALECT = "http://json-schema.org/draft-07/schema#"


@pytest.mark.parametrize(
    "schema",
    [
        PLAN_STEP_SCHEMA,
        PLAN_SCHEMA,
        REPLAN_SCHEMA,
        RESULT_SCHEMA,
        REFLECTION_SCHEMA,
        CRITIQUE_SCHEMA,
    ],
)
def test_builtin_schemas_conform_without_mutation(schema: dict[str, Any]) -> None:
    """所有内置结构化定义可直接接入固定版本，无需改写发送载荷。"""
    original = copy.deepcopy(schema)
    create_schema_validator(schema)
    assert schema == original


def test_pointer_preserves_intermediate_resource_scope() -> None:
    """跨越嵌入资源的指针继续从该资源解析后续 fragment 引用。"""
    schema = {
        "$defs": {
            "r": {
                "$id": "urn:r",
                "$defs": {
                    "x": {"type": "integer"},
                    "s": {"$ref": "#/$defs/x"},
                },
            }
        },
        "$ref": "#/$defs/r/$defs/s",
    }
    validator = create_schema_validator(schema)
    assert validator.is_valid(1)
    assert not validator.is_valid("wrong")


def test_missing_anchor_rejected_before_instance_validation() -> None:
    """已知不存在的本地锚点不能等付费模型输出后才报错。"""
    with pytest.raises(SchemaError):
        create_schema_validator({"$ref": "#missing"})


@pytest.mark.parametrize(
    "schema",
    [
        {"x": 1, "$ref": "#/x/y"},
        {"a": [1], "$ref": "#/a/zz"},
        {"x": 1, "$dynamicRef": "#/x/y"},
    ],
    ids=["pointer_through_scalar", "nonnumeric_array_index", "dynamic_ref_through_scalar"],
)
def test_unescapable_pointer_paths_are_reported_as_schema_error(
    schema: dict[str, Any],
) -> None:
    """指针穿越非法内容形态时报 SchemaError，不泄漏底层 TypeError / ValueError。"""
    with pytest.raises(SchemaError):
        create_schema_validator(schema)


def test_reused_schema_is_checked_in_each_resource() -> None:
    """共享 Python 字典在不同资源中有不同引用含义，不能仅按对象去重。"""
    shared = {"$ref": "#/$defs/x"}
    schema = {
        "$defs": {
            "bad": {"$id": "urn:bad", "properties": {"value": shared}},
            "good": {"$id": "urn:good", "$defs": {"x": {"type": "integer"}}, "properties": {"value": shared}},
        },
        "$ref": "#/$defs/bad",
    }
    with pytest.raises(SchemaError):
        create_schema_validator(schema)


@pytest.mark.parametrize("identifier", ["", "#"])
def test_empty_id_keeps_parent_resource(identifier: str) -> None:
    """空标识不应错误切断 fragment 对父资源定义的引用。"""
    schema = {
        "$defs": {
            "x": {"$id": identifier, "$ref": "#/$defs/y"},
            "y": {"type": "integer"},
        },
        "$ref": "#/$defs/x",
    }
    assert create_schema_validator(schema).is_valid(1)


@pytest.mark.parametrize("declaration", [None, JSON_SCHEMA_DIALECT, f"{JSON_SCHEMA_DIALECT}#"])
def test_accepts_default_and_explicit_dialect(declaration: str | None) -> None:
    """无声明与标准声明都执行 2020-12 约束。"""
    schema: dict[str, Any] = {
        "type": "object",
        "dependentRequired": {"equipment": ["time"]},
    }
    if declaration is not None:
        schema["$schema"] = declaration
    validator = create_schema_validator(schema)

    assert isinstance(validator, Draft202012Validator)
    assert validator.is_valid({"equipment": "EQ-01", "time": "now"})
    assert not validator.is_valid({"equipment": "EQ-01"})


@pytest.mark.parametrize("declaration", [_OLD_DIALECT, "urn:unsupported:dialect", None, 12, [], {}])
def test_rejects_unsupported_or_invalid_declaration(declaration: Any) -> None:
    """显式声明不允许静默回退或将错误类型当作未声明。"""
    with pytest.raises(SchemaError):
        create_schema_validator({"$schema": declaration})


@pytest.mark.parametrize(
    "schema,valid,invalid",
    [
        (
            {"$defs": {"text": {"type": "string"}}, "$ref": "#/$defs/text", "minLength": 3},
            "abc",
            "ab",
        ),
        (
            {
                "type": "array",
                "prefixItems": [{"type": "string"}, {"type": "integer"}],
                "items": False,
                "minItems": 2,
            },
            ["EQ-01", 1],
            ["EQ-01", "1"],
        ),
        (
            {
                "type": "object",
                "allOf": [{"properties": {"equipment": {"type": "string"}}}],
                "unevaluatedProperties": False,
            },
            {"equipment": "EQ-01"},
            {"equipment": "EQ-01", "unexpected": True},
        ),
    ],
    ids=["ref_siblings", "prefix_items", "unevaluated_properties"],
)
def test_applies_202012_constraints(schema: dict[str, Any], valid: Any, invalid: Any) -> None:
    """版本差异通过实际通过与失败样本验证，而不是只检查类名。"""
    validator = create_schema_validator(schema)

    assert validator.is_valid(valid)
    assert not validator.is_valid(invalid)


@pytest.mark.parametrize("schema", [{"type": "invalid"}, {"required": "field"}, {"items": []}])
def test_checks_schema_before_validating_instance(schema: dict[str, Any]) -> None:
    """非法 Schema 在接入阶段失败，无需等待一个实例触发约束。"""
    with pytest.raises(SchemaError):
        create_schema_validator(schema)


# 元校验递归下降在 CPython 3.14 默认递归上限下约 100 层触顶；取远高于该阈值的深度，
# 使断言只依赖「耗尽递归深度」这一事实，而不依赖具体解释器的每层栈帧数。
_DEEP_NESTING = 400


def test_deeply_nested_schema_is_reported_as_schema_error() -> None:
    """极深嵌套耗尽元校验递归深度时仍报 SchemaError，不泄漏 RecursionError。"""
    schema: dict[str, Any] = {"type": "object"}
    for _ in range(_DEEP_NESTING):
        schema = {"type": "object", "properties": {"child": schema}}

    with pytest.raises(SchemaError):
        create_schema_validator(schema)


@pytest.mark.parametrize("schema,expected", [(True, True), (False, False)])
def test_accepts_boolean_schema(schema: bool, expected: bool) -> None:
    """布尔 Schema 保持全接受或全拒绝语义。"""
    assert create_schema_validator(schema).is_valid({"value": 1}) is expected


@pytest.mark.parametrize("keyword", ["properties", "patternProperties", "$defs", "definitions", "dependentSchemas"])
def test_rejects_old_dialect_in_schema_maps(keyword: str) -> None:
    """映射型子 Schema 不能切换到旧版本，即使该分支尚未被实例使用。"""
    with pytest.raises(SchemaError):
        create_schema_validator({keyword: {"child": {"$schema": _OLD_DIALECT}}})


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf", "prefixItems"])
def test_rejects_old_dialect_in_schema_arrays(keyword: str) -> None:
    """组合分支和按位置数组中的旧版本声明不能逃过预检。"""
    with pytest.raises(SchemaError):
        create_schema_validator({keyword: [{"$schema": _OLD_DIALECT}]})


@pytest.mark.parametrize(
    "keyword",
    [
        "items",
        "contains",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    ],
)
def test_rejects_old_dialect_in_single_subschema(keyword: str) -> None:
    """单个子 Schema 位置同样遵守固定方言。"""
    with pytest.raises(SchemaError):
        create_schema_validator({keyword: {"$schema": _OLD_DIALECT}})


def test_does_not_treat_instance_data_or_property_name_as_dialect() -> None:
    """实例字面量中的 $schema/$ref 与同名业务字段不属于 Schema 声明。"""
    instance = {"$schema": _OLD_DIALECT, "$ref": "https://example.invalid/data"}
    schema = {
        "type": "object",
        "properties": {"$schema": {"type": "string"}, "$ref": {"type": "string"}},
        "const": instance,
        "enum": [instance],
        "default": instance,
        "examples": [instance],
    }
    original = copy.deepcopy(schema)

    assert create_schema_validator(schema).is_valid(instance)
    assert schema == original


def test_local_pointer_resolves_escaped_target_without_mutating_schema() -> None:
    """本地指针遵守 JSON Pointer 转义，并保持调用方 Schema 不变。"""
    schema = {
        "$defs": {"a/b~c": {"type": "integer"}},
        "$ref": "#/$defs/a~1b~0c",
    }
    original = copy.deepcopy(schema)
    validator = create_schema_validator(schema)

    assert validator.is_valid(1)
    assert not validator.is_valid("1")
    assert schema == original


def test_checks_local_reference_target_outside_standard_schema_positions() -> None:
    """被本地引用的扩展位置实际充当 Schema 时，也必须拒绝旧方言。"""
    schema = {"extension": {"$schema": _OLD_DIALECT}, "$ref": "#/extension"}

    with pytest.raises(SchemaError):
        create_schema_validator(schema)


def test_recursive_local_dynamic_reference_remains_usable() -> None:
    """本地动态锚点递归可预检终止，并对深层节点执行实际约束。"""
    schema = {
        "$dynamicAnchor": "node",
        "type": "object",
        "properties": {"value": {"type": "integer"}, "child": {"$dynamicRef": "#node"}},
    }
    validator = create_schema_validator(schema)

    assert validator.is_valid({"value": 1, "child": {"value": 2}})
    assert not validator.is_valid({"value": 1, "child": {"value": "2"}})


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("reference", ["https://example.invalid/schema", "other.json", "urn:example:schema"])
def test_rejects_nonfragment_references_before_resolution(keyword: str, reference: str) -> None:
    """非 fragment 引用在创建时被拒绝，无需远程获取或实例触发。"""
    with pytest.raises(SchemaError):
        create_schema_validator({keyword: reference})


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef"])
@pytest.mark.parametrize("reference_count", [0, 1, 10])
def test_standard_reference_targets_share_one_meta_validation(keyword: str, reference_count: int) -> None:
    """标准子树已经由根元校验覆盖，引用数量不应放大元校验次数。"""
    schema = {
        "$defs": {"value": {"type": "integer"}},
        "allOf": [{keyword: "#/$defs/value"} for _ in range(reference_count)] or [True],
    }
    with patch.object(Draft202012Validator, "check_schema", wraps=Draft202012Validator.check_schema) as check:
        validator = create_schema_validator(schema)
    assert check.call_count == 1
    assert validator.is_valid(1)
    if reference_count:
        assert not validator.is_valid("wrong")


@pytest.mark.parametrize("child_first,expected", [(False, 2), (True, 3)])
def test_extension_target_and_children_share_supplemental_meta_validation(child_first: bool, expected: int) -> None:
    """扩展目标补验一次，其标准子树和重复引用复用同次预检覆盖。"""
    references = [
        {"$ref": "#/extension"},
        {"$ref": "#/extension"},
        {"$ref": "#/extension/$defs/value"},
    ]
    if child_first:
        references.reverse()
    schema = {
        "extension": {"$defs": {"value": {"type": "integer"}}, "type": "integer"},
        "allOf": references,
    }
    with patch.object(Draft202012Validator, "check_schema", wraps=Draft202012Validator.check_schema) as check:
        validator = create_schema_validator(schema)
    # 先遇子目标时，它尚未被父目标覆盖；父目标仍须独立补验自身约束。
    assert check.call_count == expected
    assert validator.is_valid(1)
    assert not validator.is_valid("wrong")


@pytest.mark.parametrize("location", ["extension", "const", "default"])
@pytest.mark.parametrize(
    "target",
    [
        {"type": "invalid"},
        {"$ref": 12},
        {"$schema": _OLD_DIALECT},
        {"$ref": "other.json"},
        {"properties": {"child": {"required": 1}}},
        42,
    ],
)
def test_nonstandard_targets_require_full_preflight(location: str, target: Any) -> None:
    """普通数据未引用时忽略，被引用为 Schema 后必须执行完整定义检查。"""
    schema = {location: target}
    create_schema_validator(schema)
    schema["$ref"] = f"#/{location}"
    with pytest.raises(SchemaError):
        create_schema_validator(schema)
