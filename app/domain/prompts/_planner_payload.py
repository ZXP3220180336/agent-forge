"""Planner 三阶段提示词的只读语义载荷投影。

这里决定预算不足时模型仍必须看到哪些业务事实；token 计量由注入的
``ContextBudgetPort.count_tokens`` 提供，最终 provider wire 准入仍归 Integration。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

_OMISSION_REASON = "context_budget"


def _truncate(text: str, limit: int | None) -> str:
    """按字符形成稳定首尾视图；最终是否满足 token 预算由调用方计数。"""
    if limit is None or len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    marker = "…"
    if limit == 1:
        return marker
    kept = limit - len(marker)
    head = (kept + 1) // 2
    tail = kept - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"


def _omission_marker(section: str, *, characters: int) -> str:
    return f'<omitted section="{section}" characters="{max(0, characters)}" reason="{_OMISSION_REASON}"/>'


def _tool_parts(line: str) -> tuple[str, str]:
    """拆出工具身份与说明；无法识别的行整体作为工具身份保留。"""
    stripped = line.strip()
    prefix, separator, description = stripped.partition(":")
    if not separator:
        return stripped, ""
    return prefix, description.strip()


def _tool_catalog_view(tool_descriptions: str, description_limit: int | None) -> str:
    """保留全部工具名，按层级缩短说明并声明省略量。"""
    if description_limit is None:
        return tool_descriptions
    lines: list[str] = []
    omitted = 0
    for raw_line in tool_descriptions.splitlines():
        name, description = _tool_parts(raw_line)
        compact = _truncate(description, description_limit)
        omitted += max(0, len(description) - len(compact))
        lines.append(f"{name}: {compact}" if compact else name)
    rendered = "\n".join(lines)
    if omitted:
        marker = _omission_marker("tool_descriptions", characters=omitted)
        rendered = f"{rendered}\n{marker}" if rendered else marker
    return rendered


def _tool_reference(call: dict[str, Any], argument_limit: int | None) -> tuple[str, int]:
    """把 ReAct 工具调用投影成可追溯的工具名与查询参数。"""
    function = call.get("function") or {}
    name = str(function.get("name") or call.get("tool") or "")
    arguments: Any = function.get("arguments")
    if arguments is None:
        arguments = call.get("params", {})
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    compact = _truncate(arguments, argument_limit)
    reference = f"{name}({compact})" if compact else name
    return reference, max(0, len(arguments) - len(compact))


def _step_line(
    record: dict[str, Any],
    *,
    description_limit: int | None,
    result_limit: int | None,
    argument_limit: int | None,
    error_limit: int | None,
) -> tuple[str, int]:
    """生成单步审计视图，并返回被省略的字符数。"""
    description = str(record.get("description") or "")
    result = str(record.get("summary") or record.get("content") or "")
    error = str(record.get("error") or "")
    compact_description = _truncate(description, description_limit)
    compact_result = _truncate(result, result_limit)
    compact_error = _truncate(error, error_limit)
    # 失败调用只能说明尝试过什么，不能成为结论的 supporting evidence。
    reference_parts = (
        [_tool_reference(call, argument_limit) for call in record.get("tool_calls", [])]
        if record.get("success")
        else []
    )
    references = [reference for reference, _ in reference_parts]
    status = "成功" if record.get("success") else "失败"
    parts = [
        f"[步骤 {record.get('id')}]",
        f"description={compact_description}",
        f"depends_on={record.get('depends_on', [])}",
        f"status={status}",
    ]
    if compact_result:
        parts.append(f"result={compact_result}")
    if compact_error:
        parts.append(f"error={compact_error}")
    if references:
        parts.append(f"evidence={','.join(references)}")
    omitted = (
        max(0, len(description) - len(compact_description))
        + max(0, len(result) - len(compact_result))
        + max(0, len(error) - len(compact_error))
        + sum(omitted_args for _, omitted_args in reference_parts)
    )
    return " ".join(parts), omitted


def _step_results_view(
    executed: list[dict[str, Any]],
    *,
    description_limit: int | None,
    result_limit: int | None,
    argument_limit: int | None,
    error_limit: int | None,
) -> str:
    """按原执行顺序保留所有步骤骨架，避免头部总截断删除后部事实。"""
    lines: list[str] = []
    omitted = 0
    for record in executed:
        line, omitted_chars = _step_line(
            record,
            description_limit=description_limit,
            result_limit=result_limit,
            argument_limit=argument_limit,
            error_limit=error_limit,
        )
        lines.append(line)
        omitted += omitted_chars
    rendered = "\n".join(lines)
    if omitted:
        marker = _omission_marker("step_results", characters=omitted)
        rendered = f"{rendered}\n{marker}" if rendered else marker
    return rendered


def _failed_step_view(failed_step: dict[str, Any], text_limit: int | None) -> str:
    """失败步只投影重规划所需字段，避免重复携带完整执行 transcript。"""
    projection = {
        "id": failed_step.get("id"),
        "description": _truncate(str(failed_step.get("description") or ""), text_limit),
        "depends_on": failed_step.get("depends_on", []),
        "success": failed_step.get("success"),
        "error": _truncate(str(failed_step.get("error") or ""), text_limit),
    }
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":"))


def _fits(
    values: tuple[str, ...],
    available_tokens: int,
    count_tokens: Callable[[str], int],
) -> bool:
    return sum(count_tokens(value) for value in values) <= max(0, available_tokens)


def serialize_planning_sections(
    goal: str,
    tool_descriptions: str,
    *,
    available_tokens: int | None,
    count_tokens: Callable[[str], int] | None,
) -> tuple[str, str]:
    """规划入口：目标不可丢；工具说明先降级，所有工具身份始终保留。"""
    if available_tokens is None or count_tokens is None:
        return goal, tool_descriptions
    for description_limit in (None, 160, 64, 0):
        tools = _tool_catalog_view(tool_descriptions, description_limit)
        if _fits((goal, tools), available_tokens, count_tokens):
            return goal, tools
    # 最小业务骨架也超限：原样返回，由策略本地预检形成零调用 Guard。
    return goal, _tool_catalog_view(tool_descriptions, 0)


def serialize_replan_sections(
    goal: str,
    tool_descriptions: str,
    executed: list[dict[str, Any]],
    failed_step: dict[str, Any],
    error: str,
    *,
    available_tokens: int | None,
    count_tokens: Callable[[str], int] | None,
) -> tuple[str, str, str, str, str]:
    """重规划入口：保留目标、失败根因、依赖和成功步骤的证据引用。"""
    levels = (
        (None, None, None, None, None),
        (96, 160, 220, 96, 160),
        (32, 100, 100, 48, 100),
        (0, 64, 48, 24, 64),
    )
    if available_tokens is None or count_tokens is None:
        levels = levels[:1]
    for tool_limit, desc_limit, result_limit, arg_limit, error_limit in levels:
        values = (
            goal,
            _tool_catalog_view(tool_descriptions, tool_limit),
            _step_results_view(
                executed,
                description_limit=desc_limit,
                result_limit=result_limit,
                argument_limit=arg_limit,
                error_limit=error_limit,
            ),
            _failed_step_view(failed_step, error_limit),
            _truncate(error, error_limit),
        )
        if available_tokens is None or count_tokens is None or _fits(values, available_tokens, count_tokens):
            return values
    return values


def serialize_summarize_sections(
    goal: str,
    executed: list[dict[str, Any]],
    *,
    available_tokens: int | None,
    count_tokens: Callable[[str], int] | None,
) -> tuple[str, str]:
    """汇总入口：保留目标和全部步骤骨架，成功结果才作为证据候选。"""
    levels = (
        (None, None, None, None),
        (160, 220, 96, 160),
        (100, 100, 48, 100),
        (64, 48, 24, 64),
    )
    if available_tokens is None or count_tokens is None:
        levels = levels[:1]
    for desc_limit, result_limit, arg_limit, error_limit in levels:
        step_results = _step_results_view(
            executed,
            description_limit=desc_limit,
            result_limit=result_limit,
            argument_limit=arg_limit,
            error_limit=error_limit,
        )
        values = (goal, step_results)
        if available_tokens is None or count_tokens is None or _fits(values, available_tokens, count_tokens):
            return values
    return values
