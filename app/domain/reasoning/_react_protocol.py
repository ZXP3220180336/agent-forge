"""ReAct 工具调用身份、终止工具与动作指纹的纯协议转换。"""

from __future__ import annotations

import json

from jsonschema.exceptions import best_match

from app.shared.json_schema import create_schema_validator

_FINAL_ANSWER_TOOL = "final_answer"


def build_final_answer_tool(schema: dict) -> dict:
    """构造以 output schema 为参数的 final_answer 工具定义。"""
    return {
        "type": "function",
        "function": {
            "name": _FINAL_ANSWER_TOOL,
            "description": (
                "完成任务后调用一次，以符合给定 JSON Schema 的结构化格式提交最终答案。不得与其他工具混用。"
            ),
            "parameters": schema,
        },
    }


def tool_call_identity_error(tool_calls: list[dict]) -> str | None:
    """在协议历史或真实执行前验证当前批次的工具调用身份。"""
    seen: set[str] = set()
    for tool_call in tool_calls:
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            return "工具调用 id 缺失（协议异常）"
        if call_id in seen:
            return f"工具调用 id 在当前批次重复: {call_id}（协议异常）"
        seen.add(call_id)
    return None


def extract_final_answer(
    tool_call: dict,
    schema: dict,
) -> tuple[dict | None, str | None]:
    """解析并校验 final_answer 参数；成功时错误为 None。"""
    try:
        arguments = json.loads(tool_call["function"]["arguments"])
    except (json.JSONDecodeError, KeyError) as error:
        return None, f"参数 JSON 解析失败: {error}"
    if not isinstance(arguments, dict):
        return None, "参数应为 JSON 对象"
    try:
        error = best_match(create_schema_validator(schema).iter_errors(arguments))
        if error is not None:
            raise error
    except Exception as error:  # noqa: BLE001 — 校验失败必须回喂模型自纠
        return None, f"不符合 schema: {error}"
    return arguments, None


def action_fingerprint(tool_calls: list[dict]) -> str:
    """按调用顺序序列化工具名和规范化参数，供停滞检测。"""
    signature = []
    for tool_call in tool_calls:
        name = tool_call["function"]["name"]
        try:
            arguments = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError, KeyError:
            arguments = tool_call.get("function", {}).get("arguments", "")
        signature.append((name, arguments))
    return json.dumps(
        signature,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
