# ============================================
# domain/prompts/manager.py - 提示词管理器
# ============================================

import json

from .templates.planning import PLANNING_PROMPT, REPLAN_PROMPT, SUMMARIZE_PROMPT
from .templates.reflection import CRITIQUE_PROMPT, REFINE_PROMPT
from .templates.system import SYSTEM_PROMPT
from .templates.tools import TOOL_FORMAT_PROMPT

# 证据链序列化截断上限（critic/refine 上下文防膨胀；证据链主体在收集阶段已受 context_budget 护栏）
_EVIDENCE_MAX_CHARS = 4000
# 单条工具记录结果截断（对齐 react.py 工具结果回喂截断基线）
_EVIDENCE_RESULT_MAX_CHARS = 500


def _serialize_evidence(evidence: list[dict]) -> str:
    """证据链记录序列化为 critic 可见文本。

    剔除 final_answer 条目（终止工具非真实证据）；单条截断 + 总量截断防上下文膨胀。
    """
    lines: list[str] = []
    for rec in evidence:
        if rec.get("tool") == "final_answer":
            continue
        params = json.dumps(rec.get("params", {}), ensure_ascii=False)
        result = str(rec.get("result", ""))[:_EVIDENCE_RESULT_MAX_CHARS]
        lines.append(
            f"[{rec.get('tool')}] params={params} result={result} "
            f"success={rec.get('success')}"
        )
    return "\n".join(lines)[:_EVIDENCE_MAX_CHARS]


def _serialize_step_results(executed: list[dict]) -> str:
    """步骤执行记录序列化为 replan/summarize 可见文本。

    每条 = 编号 + 成败 + 产出摘要 + 工具记录引用；单条截断 + 总量截断防膨胀。
    """
    lines: list[str] = []
    for rec in executed:
        summary = str(rec.get("summary") or rec.get("content") or "")[
            :_EVIDENCE_RESULT_MAX_CHARS
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
    def build_reflection_critique_prompt(evidence: list[dict], draft: dict) -> str:
        """构建 Reflection 自查指令：证据链 + 初稿 + 穷举清单。"""
        return CRITIQUE_PROMPT.format(
            evidence=_serialize_evidence(evidence),
            draft=json.dumps(draft, ensure_ascii=False),
        )

    @staticmethod
    def build_reflection_refine_prompt(
        evidence: list[dict],
        draft: dict,
        issues: list[dict],
    ) -> str:
        """构建 Reflection 修正指令：证据链 + 初稿 + 审查意见。"""
        return REFINE_PROMPT.format(
            evidence=_serialize_evidence(evidence),
            draft=json.dumps(draft, ensure_ascii=False),
            issues=json.dumps(issues, ensure_ascii=False),
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
