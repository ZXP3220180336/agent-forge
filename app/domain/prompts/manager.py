# ============================================
# domain/prompts/manager.py - 提示词管理器
# ============================================

import json
import re
from collections.abc import Callable
from typing import Any

from .templates.planning import PLANNING_PROMPT, REPLAN_PROMPT, SUMMARIZE_PROMPT
from .templates.reflection import CRITIQUE_PROMPT, REFINE_PROMPT
from .templates.system import SYSTEM_PROMPT
from .templates.tools import TOOL_FORMAT_PROMPT

# 兼容旧调用方的字符上限；Reflection 正式路径使用模型 token 计数。
_EVIDENCE_MAX_CHARS = 4000
_STEP_RESULT_MAX_CHARS = 500
# 单条记录不能挤占整份报告；全局预算仍由调用方注入的计数器裁决。
_EVIDENCE_RESULT_MAX_TOKENS = 180
# 证据是 RCA 可验证性的基础，因此审查与修订都给它最高基础份额；
# 修订还必须为问题清单留出独立空间，避免严重缺陷被候选稿淹没。
_REFLECTION_CRITIQUE_WEIGHTS = {"evidence": 0.60, "draft": 0.40}
_REFLECTION_REFINE_WEIGHTS = {"evidence": 0.45, "draft": 0.35, "issues": 0.20}
# 时间和量测值是跨来源关联良率、设备与工艺事件的关键锚点。
_ANCHOR_PATTERN = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?\b"
    r"|(?<![\w.])[+-]?\d+(?:\.\d+)?\s*(?:%|ppm|ppb|ms|°C|℃|V|A|W|Hz|nm|μm|um|mm)"
)


def _raw_evidence(evidence: list[dict]) -> str:
    """生成预算估算用的完整文本；不会修改或替代原始证据链。"""
    lines: list[str] = []
    for index, record in enumerate(evidence, start=1):
        if record.get("tool") == "final_answer":
            continue
        lines.append(
            f"[E{index:04d}] tool={record.get('tool', '')} "
            f"params={json.dumps(record.get('params', {}), ensure_ascii=False)} "
            f"success={record.get('success')} error_code={record.get('error_code')} "
            f"result={record.get('result') or record.get('error') or ''}"
        )
    return "\n".join(lines)


def _count_list_items(value: Any) -> int:
    """统计嵌套清单项，用于如实报告缩减掉了多少业务条目。"""
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
        # 标记置于 JSON 外，避免伪造候选稿或 issue schema 中不存在的业务字段。
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
    """按业务优先级初分配；未使用额度回流给仍装不下的高价值字段。"""
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


def _serialize_step_results(executed: list[dict]) -> str:
    """步骤执行记录序列化为 replan/summarize 可见文本。

    每条 = 编号 + 成败 + 产出摘要 + 工具记录引用；单条截断 + 总量截断防膨胀。
    """
    lines: list[str] = []
    for rec in executed:
        summary = str(rec.get("summary") or rec.get("content") or "")[
            :_STEP_RESULT_MAX_CHARS
        ]
        status = "成功" if rec.get("success") else f"失败({rec.get('error', '')})"
        tools = ",".join(
            str(tc.get("function", {}).get("name", ""))
            for tc in rec.get("tool_calls", [])
        )
        lines.append(
            f"[步骤 {rec.get('id')}] {str(rec.get('description', ''))[:120]}"
            f" → {status} 产出={summary} 工具={tools or '无'}"
        )
    return "\n".join(lines)[:_EVIDENCE_MAX_CHARS]


def _omission_marker(section: str, unit: str, count: int) -> str:
    """显式声明业务信息被省略，防止模型把缩减视图误判为完整记录。"""
    return f'<omitted section="{section}" {unit}="{count}" reason="context_budget"/>'


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
        # 同一记录逐级降采样，尽量保留“记录存在及其来源”，再考虑整条丢弃。
        candidate_lines = (
            lines[index],
            _evidence_line(
                index,
                record,
                count_tokens,
                params_tokens=32,
                result_tokens=48,
            ),
            _evidence_line(
                index,
                record,
                count_tokens,
                params_tokens=16,
                result_tokens=0,
            ),
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


class PromptManager:
    """
    提示词管理器
    负责组装不同场景下的完整提示词。
    """

    @staticmethod
    def build_system_prompt(tool_descriptions: str = "") -> str:
        """构建系统提示词"""
        prompt = SYSTEM_PROMPT
        if tool_descriptions:
            prompt += f"\n\n{TOOL_FORMAT_PROMPT.format(tools=tool_descriptions)}"
        return prompt

    @staticmethod
    def build_reflection_critique_prompt(
        evidence: list[dict],
        draft: dict,
        *,
        max_tokens: int | None = None,
        count_tokens: Callable[[str], int] | None = None,
    ) -> str:
        """构建 Reflection 自查指令；可按 token 预算生成只读语义视图。"""
        if max_tokens is None or count_tokens is None:
            evidence_text = _serialize_evidence(
                evidence,
                draft,
                max_tokens=_EVIDENCE_MAX_CHARS,
                count_tokens=len,
            )
            draft_text = json.dumps(draft, ensure_ascii=False)
        else:
            # 先扣除固定模板，动态业务内容只能使用真实剩余的上下文容量。
            empty = CRITIQUE_PROMPT.format(evidence="", draft="")
            available = max(0, max_tokens - count_tokens(empty))
            raw_sections = {
                "evidence": _raw_evidence(evidence),
                "draft": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
            }
            budgets = _allocate_budgets(
                available,
                raw_sections,
                _REFLECTION_CRITIQUE_WEIGHTS,
                count_tokens,
            )
            evidence_text = _serialize_evidence(
                evidence,
                draft,
                max_tokens=budgets["evidence"],
                count_tokens=count_tokens,
            )
            draft_text = _serialize_json_section(
                draft,
                section="draft",
                max_tokens=budgets["draft"],
                count_tokens=count_tokens,
            )
        return CRITIQUE_PROMPT.format(
            evidence=evidence_text,
            draft=draft_text,
        )

    @staticmethod
    def build_reflection_refine_prompt(
        evidence: list[dict],
        draft: dict,
        issues: list[dict],
        *,
        max_tokens: int | None = None,
        count_tokens: Callable[[str], int] | None = None,
    ) -> str:
        """构建 Reflection 修正指令；critical issue 与被引用证据优先。"""
        if max_tokens is None or count_tokens is None:
            evidence_text = _serialize_evidence(
                evidence,
                draft,
                max_tokens=_EVIDENCE_MAX_CHARS,
                count_tokens=len,
            )
            draft_text = json.dumps(draft, ensure_ascii=False)
            issues_text = json.dumps(issues, ensure_ascii=False)
        else:
            # 修订必须同时看到证据、当前稿和待解决问题，三者分别受预算约束。
            empty = REFINE_PROMPT.format(evidence="", draft="", issues="")
            available = max(0, max_tokens - count_tokens(empty))
            raw_sections = {
                "evidence": _raw_evidence(evidence),
                "draft": json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                "issues": json.dumps(issues, ensure_ascii=False, separators=(",", ":")),
            }
            budgets = _allocate_budgets(
                available,
                raw_sections,
                _REFLECTION_REFINE_WEIGHTS,
                count_tokens,
            )
            evidence_text = _serialize_evidence(
                evidence,
                draft,
                max_tokens=budgets["evidence"],
                count_tokens=count_tokens,
            )
            draft_text = _serialize_json_section(
                draft,
                section="draft",
                max_tokens=budgets["draft"],
                count_tokens=count_tokens,
            )
            issues_text = _serialize_json_section(
                issues,
                section="issues",
                max_tokens=budgets["issues"],
                count_tokens=count_tokens,
                prioritize_issues=True,
            )
        return REFINE_PROMPT.format(
            evidence=evidence_text,
            draft=draft_text,
            issues=issues_text,
        )

    @staticmethod
    def build_planning_prompt(user_input: str, tool_descriptions: str) -> str:
        """构建 Planner 规划指令：用户目标 + 工具目录（introspection 文本，不入 tools）。"""
        return PLANNING_PROMPT.format(
            goal=user_input,
            tool_descriptions=tool_descriptions,
        )

    @staticmethod
    def build_planning_replan_prompt(
        goal: str,
        tool_descriptions: str,
        executed: list[dict],
        failed_step: dict,
        error: str,
    ) -> str:
        """构建 Planner 重规划指令：已完成步骤 + 失败步骤 + 原因。"""
        return REPLAN_PROMPT.format(
            goal=goal,
            tool_descriptions=tool_descriptions,
            executed=_serialize_step_results(executed),
            failed_step=json.dumps(failed_step, ensure_ascii=False),
            error=error,
        )

    @staticmethod
    def build_planning_summarize_prompt(goal: str, executed: list[dict]) -> str:
        """构建 Planner 汇总指令：各步骤结果 → 证据链报告。"""
        return SUMMARIZE_PROMPT.format(
            goal=goal,
            step_results=_serialize_step_results(executed),
        )
