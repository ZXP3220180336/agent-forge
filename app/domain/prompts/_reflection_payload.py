"""Reflection 提示词动态载荷的选择与语义缩减。"""

import json
import re
from collections.abc import Callable
from typing import Any

__all__ = ["serialize_critique_sections", "serialize_refine_sections"]

# 兼容路径与单条证据上限彼此独立，避免与 Planner 的字符限制形成假共享。
_LEGACY_EVIDENCE_MAX_CHARS = 4000
_EVIDENCE_RESULT_MAX_TOKENS = 180
# 证据是 RCA 可验证性的基础；修订阶段还必须为问题清单预留独立空间。
_CRITIQUE_WEIGHTS = {"evidence": 0.60, "draft": 0.40}
_REFINE_WEIGHTS = {"evidence": 0.45, "draft": 0.35, "issues": 0.20}
# 时间和量测值用于跨来源关联良率、设备与工艺事件。
_ANCHOR_PATTERN = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b"
    r"|(?<![\w.])[+-]?\d+(?:\.\d+)?\s*(?:%|ppm|ppb|ms|°C|℃|V|A|W|Hz|nm|μm|um|mm)"
)


def _omission_marker(section: str, unit: str, count: int) -> str:
    """显式声明业务信息被省略，防止模型把缩减视图误判为完整记录。"""
    return f'<omitted section="{section}" {unit}="{count}" reason="context_budget"/>'


def _truncate_to_tokens(
    text: str,
    max_tokens: int,
    count_tokens: Callable[[str], int],
) -> str:
    """按 token 上限保留首尾；首部常含对象身份，尾部常含结果或错误。"""
    if count_tokens(text) <= max_tokens:
        return text
    marker = '…<omitted reason="context_budget"/>…'
    if max_tokens <= 0 or count_tokens(marker) > max_tokens:
        return marker
    low, high = 0, len(text)
    best = marker
    while low <= high:
        kept = (low + high) // 2
        head = (kept + 1) // 2
        tail = kept - head
        candidate = f"{text[:head]}{marker}{text[len(text) - tail :] if tail else ''}"
        if count_tokens(candidate) <= max_tokens:
            best = candidate
            low = kept + 1
        else:
            high = kept - 1
    return best


def _supporting_evidence_refs(value: Any) -> list[str]:
    """递归收集候选稿声明的证据引用，作为证据保留的第一优先级。"""
    refs: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "supporting_evidence" and isinstance(item, list):
                refs.extend(str(ref) for ref in item)
            else:
                refs.extend(_supporting_evidence_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_supporting_evidence_refs(item))
    return refs


def _evidence_is_referenced(record: dict, refs: list[str]) -> bool:
    """以工具名和查询参数判断引用是否指向该条证据记录。"""
    tool = str(record.get("tool", "")).lower()
    values = [str(value).lower() for value in (record.get("params") or {}).values()]
    return any(
        tool
        and tool in ref.lower()
        and (not values or any(value in ref.lower() for value in values))
        for ref in refs
    )


def _evidence_line(
    source_index: int,
    record: dict,
    count_tokens: Callable[[str], int],
    *,
    params_tokens: int = 80,
    result_tokens: int = _EVIDENCE_RESULT_MAX_TOKENS,
) -> str:
    """生成带稳定来源序号的证据摘要，保留审查所需的追溯字段。"""
    params = _truncate_to_tokens(
        json.dumps(record.get("params", {}), ensure_ascii=False, separators=(",", ":")),
        params_tokens,
        count_tokens,
    )
    result = str(record.get("result") or record.get("error") or "")
    compact_result = _truncate_to_tokens(result, result_tokens, count_tokens)
    # 正文被压缩时单独提升时间/量测锚点，仍可检查跨系统的时序关联。
    anchors = list(dict.fromkeys(_ANCHOR_PATTERN.findall(result)))[:8]
    anchor_text = f" anchors={','.join(anchors)}" if anchors else ""
    return (
        f"[E{source_index:04d}] tool={record.get('tool', '')} params={params} "
        f"success={record.get('success')} error_code={record.get('error_code')} "
        f"result={compact_result}{anchor_text}"
    )


def _full_evidence_view(
    evidence: list[dict],
    count_tokens: Callable[[str], int],
) -> str:
    """生成与实际证据行同形的预算估算文本；不会替代原始证据链。"""
    return "\n".join(
        _evidence_line(index, record, count_tokens)
        for index, record in enumerate(evidence, start=1)
        if record.get("tool") != "final_answer"
    )


def _serialize_evidence(
    evidence: list[dict],
    draft: dict,
    *,
    max_tokens: int,
    count_tokens: Callable[[str], int],
) -> str:
    """生成只读证据视图：被当前稿引用的记录优先，其次保留最近记录。"""
    records = [
        (index, record)
        for index, record in enumerate(evidence, start=1)
        if record.get("tool") != "final_answer"
    ]
    lines = {
        index: _evidence_line(index, record, count_tokens) for index, record in records
    }
    full = "\n".join(lines[index] for index, _ in records)
    if count_tokens(full) <= max_tokens:
        return full

    refs = _supporting_evidence_refs(draft)
    # 先保住已支撑候选结论的证据；剩余容量用于最近取得的排查事实。
    referenced = [item for item in records if _evidence_is_referenced(item[1], refs)]
    referenced_ids = {index for index, _ in referenced}
    remaining = [item for item in reversed(records) if item[0] not in referenced_ids]
    selected: dict[int, str] = {}
    for index, record in [*referenced, *remaining]:
        # 同一记录逐级降采样，最后才整条省略，尽量保留来源和执行状态。
        candidate_lines = (
            lines[index],
            _evidence_line(index, record, count_tokens, params_tokens=32, result_tokens=48),
            _evidence_line(index, record, count_tokens, params_tokens=16, result_tokens=0),
        )
        candidate_ids = [*selected, index]
        omitted = len(records) - len(candidate_ids)
        for candidate_line in candidate_lines:
            candidate = {**selected, index: candidate_line}
            rendered = "\n".join(candidate[i] for i in sorted(candidate))
            if omitted:
                rendered += "\n" + _omission_marker("evidence", "records", omitted)
            if count_tokens(rendered) <= max_tokens:
                selected[index] = candidate_line
                break

    omitted = len(records) - len(selected)
    rendered = "\n".join(selected[i] for i in sorted(selected))
    if omitted:
        marker = _omission_marker("evidence", "records", omitted)
        rendered = f"{rendered}\n{marker}" if rendered else marker
    return rendered


def _count_list_items(value: Any) -> int:
    """统计嵌套清单项，用于如实报告省略条目数。"""
    if isinstance(value, list):
        return len(value) + sum(_count_list_items(item) for item in value)
    if isinstance(value, dict):
        return sum(_count_list_items(item) for item in value.values())
    return 0


def _count_string_chars(value: Any) -> int:
    """统计嵌套文本量，用于生成可审计的省略计数。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_count_string_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(_count_string_chars(item) for item in value.values())
    return 0


def _compact_value(
    value: Any,
    *,
    string_tokens: int,
    list_items: int,
    count_tokens: Callable[[str], int],
) -> Any:
    """递归缩减送审副本；调用方持有的证据、稿件和问题清单保持不变。"""
    if isinstance(value, str):
        return _truncate_to_tokens(value, string_tokens, count_tokens)
    if isinstance(value, list):
        return [
            _compact_value(
                item,
                string_tokens=string_tokens,
                list_items=list_items,
                count_tokens=count_tokens,
            )
            for item in value[:list_items]
        ]
    if isinstance(value, dict):
        return {
            key: _compact_value(
                item,
                string_tokens=string_tokens,
                list_items=list_items,
                count_tokens=count_tokens,
            )
            for key, item in value.items()
        }
    return value


def _draft_projection(draft: dict) -> dict:
    """预算不足时保留结论、证据引用及显式放弃所在的报告骨架。"""
    return {
        key: draft[key]
        for key in ("summary", "conclusions", "explicit_abstention")
        if key in draft
    }


def _serialize_json_section(
    value: Any,
    *,
    section: str,
    max_tokens: int,
    count_tokens: Callable[[str], int],
    prioritize_issues: bool = False,
) -> str:
    """把稿件或问题清单缩减为合法 JSON，并在 JSON 外附省略事实。"""
    original = value
    if prioritize_issues and isinstance(value, list):
        # critical 决定报告能否提交；minor 倒序让最近发现的问题先进入修订。
        critical = [item for item in value if item.get("severity") == "critical"]
        minor = [item for item in value if item.get("severity") != "critical"]
        value = [*critical, *reversed(minor)]
    elif section == "draft" and isinstance(value, dict):
        value = _draft_projection(value)

    raw = json.dumps(original, ensure_ascii=False, separators=(",", ":"))
    if count_tokens(raw) <= max_tokens:
        return raw

    original_items = _count_list_items(original)
    original_chars = _count_string_chars(original)
    for string_tokens, list_items in ((128, 16), (96, 8), (64, 4), (40, 2), (24, 1)):
        compact = _compact_value(
            value,
            string_tokens=string_tokens,
            list_items=list_items,
            count_tokens=count_tokens,
        )
        text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        omitted_items = original_items - _count_list_items(compact)
        omitted_chars = original_chars - _count_string_chars(compact)
        # 标记置于 JSON 外，避免伪造稿件或 issue schema 中不存在的业务字段。
        marker = (
            f'<omitted section="{section}" items="{omitted_items}" '
            f'characters="{omitted_chars}" reason="context_budget"/>'
        )
        rendered = f"{text}\n{marker}"
        if count_tokens(rendered) <= max_tokens:
            return rendered

    marker = (
        f'<omitted section="{section}" items="{original_items}" '
        f'characters="{original_chars}" reason="context_budget"/>'
    )
    return f"{{}}\n{marker}"


def _allocate_budgets(
    total_tokens: int,
    sections: dict[str, str],
    weights: dict[str, float],
    count_tokens: Callable[[str], int],
) -> dict[str, int]:
    """按业务优先级初分配；未使用额度回流给仍装不下的字段。"""
    total_tokens = max(0, total_tokens)
    needs = {key: count_tokens(value) for key, value in sections.items()}
    allocated = {key: int(total_tokens * weights[key]) for key in sections}
    for key in sections:
        allocated[key] = min(allocated[key], needs[key])
    spare = total_tokens - sum(allocated.values())
    for key in sections:
        if spare <= 0:
            break
        wanted = max(0, needs[key] - allocated[key])
        grant = min(spare, wanted)
        allocated[key] += grant
        spare -= grant
    return allocated


def serialize_critique_sections(
    evidence: list[dict],
    draft: dict,
    *,
    available_tokens: int | None,
    count_tokens: Callable[[str], int] | None,
) -> tuple[str, str]:
    """生成 Critique 模板所需的 evidence 与 draft 文本。"""
    if available_tokens is None or count_tokens is None:
        return (
            _serialize_evidence(
                evidence,
                draft,
                max_tokens=_LEGACY_EVIDENCE_MAX_CHARS,
                count_tokens=len,
            ),
            json.dumps(draft, ensure_ascii=False),
        )

    sections = {
        "evidence": _full_evidence_view(evidence, count_tokens),
        "draft": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
    }
    budgets = _allocate_budgets(available_tokens, sections, _CRITIQUE_WEIGHTS, count_tokens)
    return (
        _serialize_evidence(
            evidence,
            draft,
            max_tokens=budgets["evidence"],
            count_tokens=count_tokens,
        ),
        _serialize_json_section(
            draft,
            section="draft",
            max_tokens=budgets["draft"],
            count_tokens=count_tokens,
        ),
    )


def serialize_refine_sections(
    evidence: list[dict],
    draft: dict,
    issues: list[dict],
    *,
    available_tokens: int | None,
    count_tokens: Callable[[str], int] | None,
) -> tuple[str, str, str]:
    """生成 Refine 模板所需的 evidence、draft 与 issues 文本。"""
    if available_tokens is None or count_tokens is None:
        return (
            _serialize_evidence(
                evidence,
                draft,
                max_tokens=_LEGACY_EVIDENCE_MAX_CHARS,
                count_tokens=len,
            ),
            json.dumps(draft, ensure_ascii=False),
            json.dumps(issues, ensure_ascii=False),
        )

    sections = {
        "evidence": _full_evidence_view(evidence, count_tokens),
        "draft": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
        "issues": json.dumps(issues, ensure_ascii=False, separators=(",", ":")),
    }
    budgets = _allocate_budgets(available_tokens, sections, _REFINE_WEIGHTS, count_tokens)
    return (
        _serialize_evidence(
            evidence,
            draft,
            max_tokens=budgets["evidence"],
            count_tokens=count_tokens,
        ),
        _serialize_json_section(
            draft,
            section="draft",
            max_tokens=budgets["draft"],
            count_tokens=count_tokens,
        ),
        _serialize_json_section(
            issues,
            section="issues",
            max_tokens=budgets["issues"],
            count_tokens=count_tokens,
            prioritize_issues=True,
        ),
    )
