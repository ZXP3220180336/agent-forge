# ============================================
# domain/prompts/manager.py - 提示词管理器
# ============================================

import json
from collections.abc import Callable

from ._reflection_payload import (
    serialize_critique_sections,
    serialize_refine_sections,
)
from .templates.planning import PLANNING_PROMPT, REPLAN_PROMPT, SUMMARIZE_PROMPT
from .templates.reflection import CRITIQUE_PROMPT, REFINE_PROMPT
from .templates.system import SYSTEM_PROMPT
from .templates.tools import TOOL_FORMAT_PROMPT

_STEP_RESULT_MAX_CHARS = 500
_STEP_RESULTS_MAX_CHARS = 4000


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
    return "\n".join(lines)[:_STEP_RESULTS_MAX_CHARS]


class PromptManager:
    """提示词公开组装入口。"""

    @staticmethod
    def build_system_prompt(tool_descriptions: str = "") -> str:
        """构建系统提示词。"""
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
        available_tokens = None
        if max_tokens is not None and count_tokens is not None:
            # 先扣除固定模板，动态业务内容只能使用真实剩余的上下文容量。
            empty = CRITIQUE_PROMPT.format(evidence="", draft="")
            available_tokens = max(0, max_tokens - count_tokens(empty))
        evidence_text, draft_text = serialize_critique_sections(
            evidence,
            draft,
            available_tokens=available_tokens,
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
        available_tokens = None
        if max_tokens is not None and count_tokens is not None:
            # 修订必须同时看到证据、当前稿和待解决问题，三者分别受预算约束。
            empty = REFINE_PROMPT.format(evidence="", draft="", issues="")
            available_tokens = max(0, max_tokens - count_tokens(empty))
        evidence_text, draft_text, issues_text = serialize_refine_sections(
            evidence,
            draft,
            issues,
            available_tokens=available_tokens,
            count_tokens=count_tokens,
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
