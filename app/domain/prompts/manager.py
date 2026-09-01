# ============================================
# domain/prompts/manager.py - 提示词管理器
# ============================================

import json

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
