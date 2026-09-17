"""结构化输出的内部数据转换与校验函数。

依赖标准库、jsonschema 与共享 Schema 契约，不执行日志、模型调用或状态结算。
调用方负责校验器异常的日志和业务转换；LLMService 仍是模块对外入口。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from jsonschema.exceptions import best_match

from app.shared.json_schema import create_schema_validator

_REASK_TEMPLATE = (
    "你的上一次输出未通过 JSON Schema 校验，具体错误如下：\n{errors}\n"
    "请根据错误修正，只输出符合 schema 的 JSON 对象，"
    "不要 markdown 代码块、不要额外解释。"
)

_LOG_TRUNCATE_LIMIT = 500  # 保留既有日志摘要长度，不在 codec 写入日志。


def strict_compliant(schema: dict[str, Any]) -> dict[str, Any]:
    """递归归一 additionalProperties: true → false（strict JSON Schema 要求）。

    OpenAI strict 模式要求每个 object 节点 additionalProperties: false（递归），
    显式 true 会 400（LLM-009）。对齐 LangChain `_recursive_set_additional_properties_false`。
    返回深拷贝副本，不污染调用方 schema；本地校验仍用原 schema（保留「允许扩展」意图）。
    """
    new_schema = copy.deepcopy(schema)
    stack = [new_schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("additionalProperties") is True:
            node["additionalProperties"] = False
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(v for v in value if isinstance(v, dict))
    return new_schema


def build_json_schema_request(
    schema: dict[str, Any],
) -> dict[str, Any]:
    """
    构建 native JSON Schema 的 response_format（strict=True，LLM-009 归一）。

    无条件构建 strict json_schema；模型 / 网关不支持该格式（400）时，由调用方
    （_extract_impl 第二级 JSON mode / _call_generate 的
    is_unsupported_response_format_error）降级，本函数不做支持性判断。

    Args:
        schema: JSON Schema

    Returns:
        {"type": "json_schema", "json_schema": {"name", "strict", "schema"}}
    """
    # 尝试 native JSON Schema
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_output",
            "strict": True,
            # LLM-009：strict 下 additionalProperties: true → false 归一（副本）——
            # strict 模式禁止 true（必然 400 且被误判「模型不支持」），归一避免
            # 白打调用；本地校验仍用原 schema（保留允许扩展意图）。
            "schema": strict_compliant(schema),
        },
    }


def build_json_mode_request() -> dict[str, str]:
    """构建普通 JSON mode 请求参数（无 Schema 约束）。"""
    return {"type": "json_object"}


def enforce_no_extra_fields(schema: dict[str, Any]) -> dict[str, Any]:
    """深拷贝并递归补全 `additionalProperties: false`（问题 4）。

    - 深拷贝：不污染调用方 schema（默认补全发生在副本上）
    - 递归：对每个 object 节点补 `additionalProperties:false`，拒绝模型扩展字段
    - 显式尊重：调用方已写 `true` 的保持 `true`（不覆盖显式允许扩展的意图）

    意义：减少模型自作主张扩展接口（如业务不需要的 `user_emotion` 混入）。
    配合 Pydantic 侧 `extra="forbid"`（业务层）双保险。
    """
    new_schema = copy.deepcopy(schema)
    stack = [new_schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        # 匹配 object：type 单值 "object" 或数组含 "object"（联合类型写法 ["object","null"]）
        node_type = node.get("type")
        is_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
        if is_object and "additionalProperties" not in node:
            node["additionalProperties"] = False
        # 递归属性定义与子结构
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(v for v in value if isinstance(v, dict))
    return new_schema


def parse_and_validate(
    content: str,
    schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """解析 + 校验，返回 (结果, 错误列表)。

    返回的二元组供回喂循环（问题 3）使用：
    - parsed 非 None = 成功（校验通过），错误列表空
    - parsed 为 None = 失败，errors 携带人话错误（供回喂；日志使用独立摘要）
    """
    try:
        parsed = json.loads(content)
    except Exception as e:  # noqa: BLE001
        return None, [f"- 顶层 JSON 解析失败：{e}"]

    if not isinstance(parsed, dict):
        return None, ["- 顶层不是 JSON 对象（应为 dict）"]

    if schema is not None:
        errors = collect_schema_errors(parsed, schema)
        if errors:
            return None, errors

    return parsed, []


def collect_schema_errors(parsed: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """收集全部 Schema 校验错误，格式化为「字段路径: message」的人话（问题 3）。

    一次收集全部错误（iter_errors 全量，非第一条），让模型一次改完。
    返回空列表 = 校验通过。

    注意：错误文本含 `e.message`（嵌入完整实例值）——用于**回喂模型**（模型
    需要看到具体错误才能修正）。写入日志需用脱敏版 `collect_schema_error_summaries`。

    LLM-007：schema 非法（UnknownType / SchemaError / TypeError 等）时
    Schema 预检或 iter_errors 的异常交由 structured 记录并转换，
    本函数不捕获、不写日志，也不决定是否降级。
    """
    errors = []
    for e in create_schema_validator(schema).iter_errors(parsed):
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errors.append(f"- 字段 `{path}`：{e.message}")
    return errors


def collect_schema_error_summaries(parsed: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """收集 Schema 校验错误的**脱敏摘要**（字段路径 + validator + 约束值）。

    与 `collect_schema_errors`（回喂模型，含 `e.message` 嵌入完整实例值）的区别：
    本函数只含 schema 结构信息（validator 名 / 约束值 / 字段路径），**无实例数据**，
    可安全写入日志——Yield RCA 场景的敏感数据（良率/晶圆）不因错误日志落盘。

    LLM-007：schema 非法时异常交由 structured 记录并转换，本函数不写日志。
    """
    summaries = []
    for e in create_schema_validator(schema).iter_errors(parsed):
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        summaries.append(f"- 字段 `{path}`：违反 `{e.validator}`={e.validator_value}")
    return summaries


def build_reask_messages(
    messages: list[dict],
    raw_content: str,
    error_text: str,
) -> list[dict]:
    """构造错误回喂的消息（问题 3）：clone + 保留上次失败输出 + 末尾追加反馈。

    - clone：`[dict(m) for m in messages]` 浅拷贝，绝不污染调用方 messages
    - 保留失败输出：assistant 消息留在历史里，让模型看到自己错在哪（self-correction 关键）
    - 末尾追加 user 消息：具体错误 + 修正指令（一次性指令，非 system 恒定规则）
    """
    new_messages = [dict(m) for m in messages]
    if raw_content:
        new_messages.append({"role": "assistant", "content": raw_content})
    new_messages.append({"role": "user", "content": _REASK_TEMPLATE.format(errors=error_text)})
    return new_messages


def truncate_json_for_log(value: dict[str, Any]) -> str:
    """将模型输出序列化并截断到安全长度（防业务敏感数据全量落盘）。

    结构化输出可能含业务敏感数据（Yield RCA 场景为良率/晶圆数据），全量写入
    WARNING 日志是潜在泄露面。截断保留前 N 字符（`_LOG_TRUNCATE_LIMIT`），
    足以定位解析/校验问题，同时避免敏感内容完整落盘。
    """
    return truncate_text_for_log(json.dumps(value, ensure_ascii=False))


def truncate_text_for_log(text: str) -> str:
    """将任意文本截断到安全长度（防业务敏感数据全量落盘）。"""
    if len(text) > _LOG_TRUNCATE_LIMIT:
        return f"{text[:_LOG_TRUNCATE_LIMIT]}...（已截断，共 {len(text)} 字符）"
    return text


def parse_json_object(content: str) -> dict[str, Any] | None:
    """解析顶层 JSON 对象，语法错误或非对象返回 None。"""
    try:
        parsed = json.loads(content)
    except Exception:  # noqa: BLE001  保留既有解析失败出口。
        return None
    return parsed if isinstance(parsed, dict) else None


def validate_schema(parsed: dict[str, Any], schema: dict[str, Any]) -> None:
    """按固定 2020-12 校验 fallback 候选，失败原样抛给观测边界。

    保留 jsonschema.validate 的 best_match 代表错误选择，不自动切换版本，
    不在函数内转换失败或写入日志。

    Args:
        parsed: 已解析的 JSON 对象。
        schema: 调用方的本地校验 schema。

    Raises:
        jsonschema.ValidationError: 候选对象不符合 schema。
        jsonschema.SchemaError: schema 本身不合法。
    """
    error = best_match(create_schema_validator(schema).iter_errors(parsed))
    if error is not None:
        raise error
