"""本地 JSON Schema 契约：固定 Draft 2020-12，不执行 I/O 或错误呈现。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from jsonschema.protocols import Validator
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def create_schema_validator(schema: dict[str, Any] | bool) -> Validator:
    """预检本地 Schema 并构建固定版本的校验器，不修改调用方对象。

    Args:
        schema: 未声明版本或明确声明 2020-12 的 Schema；只支持内嵌及 fragment 引用。

    Returns:
        未启用 format 断言的 Draft202012Validator，错误呈现由调用方负责。

    Raises:
        SchemaError: 定义非法、版本不支持、本地引用不可解析或嵌套超出预检深度。
    """
    _check_schema_definition(schema)
    checked: set[int] = set()
    _check_schema_declarations(schema, checked)
    registry = Registry()
    resolver = registry.resolver_with_root(DRAFT202012.create_resource(schema))
    pending = [(schema, resolver)]
    references = []
    visited: set[tuple[int, int]] = set()
    while pending or references:
        if not pending:
            reference, source_resolver = references.pop()
            try:
                resolved = source_resolver.lookup(reference)
            # referencing 的指针穿越未防护全部内容形态：穿标量抛 TypeError，
            # 数组遇非数字下标抛 ValueError（越界下标与缺失键已由其转为 Unresolvable）。
            # 预检对外只暴露 SchemaError，这些一并连同异常链收编。
            except (Unresolvable, TypeError, ValueError) as error:
                raise SchemaError(f"无法解析本地 Schema 引用：{reference}") from error
            # 根元校验已覆盖标准子树；只为扩展位置的未覆盖目标补验。
            # 声明检查按对象去重，下面的引用检查仍需区分资源作用域。
            if id(resolved.contents) not in checked:
                _check_schema_definition(resolved.contents)
                _check_schema_declarations(resolved.contents, checked)
            pending.append((resolved.contents, resolved.resolver))
        node, node_resolver = pending.pop()
        if isinstance(node, bool):
            continue
        # 同一个字典可被不同 $id 资源复用；仅按字典 identity 去重会漏检引用。
        resource_root = node_resolver.lookup("#").contents
        identity = (id(node), id(resource_root))
        if identity in visited:
            continue
        visited.add(identity)
        # 使用规范库定位真正子 Schema；const/default 等数据与业务字段名不会被误判。
        for child in DRAFT202012.subresources_of(node):
            child_resource = DRAFT202012.create_resource(child)
            pending.append((child, node_resolver.in_subresource(child_resource)))
        for keyword in ("$ref", "$dynamicRef"):
            if keyword not in node:
                continue
            reference = node[keyword]
            references.append((reference, node_resolver))
    # 显式空 Registry 禁止库默认远程获取；引用作用域由同一规范库解释。
    return Draft202012Validator(schema, registry=registry)


def _check_schema_definition(schema: dict[str, Any] | bool) -> None:
    """元校验覆盖当前根及标准子 Schema，统一递归深度失败出口。"""
    try:
        Draft202012Validator.check_schema(schema)
    except RecursionError as error:
        # 元校验是递归下降：极深嵌套先耗尽递归深度而非给出 SchemaError。
        # 预检对外的失败契约只有 SchemaError，此处统一收编，不让 RecursionError 逃逸。
        raise SchemaError("Schema 嵌套过深，无法预检") from error


def _check_schema_declarations(schema: dict[str, Any] | bool, checked: set[int]) -> None:
    """扫描已通过元校验的子树声明，累计本次预检覆盖，不跨调用缓存。"""
    # 子 Schema 由规范库按 Mapping 返回；身份去重依赖对象引用，不做拷贝转换
    pending: list[Mapping[str, Any] | bool] = [schema]
    while pending:
        node = pending.pop()
        if id(node) in checked:
            continue
        checked.add(id(node))
        if isinstance(node, bool):
            continue
        if "$schema" in node and node["$schema"] not in (
            JSON_SCHEMA_DIALECT,
            JSON_SCHEMA_DIALECT + "#",
        ):
            raise SchemaError(f"不支持的 JSON Schema 版本 {node['$schema']!r}；仅支持 {JSON_SCHEMA_DIALECT}")
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in node and not node[keyword].startswith("#"):
                raise SchemaError(f"{keyword} 仅支持文档内 fragment 引用：{node[keyword]!r}")
        pending.extend(DRAFT202012.subresources_of(node))
