"""PromptManager 提示词构建与语义缩减单元测试。"""

import re
from copy import deepcopy

from app.domain.prompts.manager import PromptManager

PLAN = {
    "goal": "分析批次 A 良率下降原因",
    "steps": [
        {"description": "查询批次 A 日良率曲线", "depends_on": []},
        {"description": "判断异常日期", "depends_on": [1]},
    ],
}

EXECUTED = [
    {
        "id": 1,
        "description": "查询批次 A 日良率曲线",
        "success": True,
        "summary": "良率 89%→80%",
        "content": "良率 89%→80%",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "yield_query", "arguments": "{}"},
            }
        ],
        "error": None,
    },
    {
        "id": 2,
        "description": "判断异常日期",
        "success": False,
        "summary": "",
        "content": "",
        "tool_calls": [],
        "error": "工具超时",
    },
]

TOOLS = "- yield_query: 查询批次良率数据\n- trend_analyze: 趋势分析"


def test_build_planning_prompt_renders_goal_and_catalog():
    """规划 prompt：用户目标 + 工具目录（introspection）+ 关键约束。"""
    prompt = PromptManager.build_planning_prompt("分析批次 A 良率下降原因", TOOLS)
    assert "分析批次 A 良率下降原因" in prompt
    assert "yield_query" in prompt  # introspection 工具目录文本
    assert "depends_on" in prompt  # 依赖建模约束
    assert "JSON Schema" in prompt  # 结构化输出约束


def test_build_planning_replan_prompt_renders_state():
    """重规划 prompt：已完成步骤摘要 + 失败步骤 + 失败原因。"""
    failed = EXECUTED[1]
    prompt = PromptManager.build_planning_replan_prompt(
        PLAN["goal"], TOOLS, EXECUTED, failed, "工具超时"
    )
    assert PLAN["goal"] in prompt
    assert "步骤 1" in prompt and "步骤 2" in prompt  # executed 摘要
    assert "工具超时" in prompt  # 失败原因
    assert "yield_query" in prompt  # 工具记录引用（evidence）


def test_build_planning_summarize_prompt_renders_results():
    """汇总 prompt：各步骤结果（含失败标记）。"""
    prompt = PromptManager.build_planning_summarize_prompt(PLAN["goal"], EXECUTED)
    assert PLAN["goal"] in prompt
    assert "良率 89%→80%" in prompt  # 步骤产出摘要
    assert "失败" in prompt  # 失败步标记（不作为证据）
    assert "explicit_abstention" in prompt  # 显式放弃规范


def test_build_reflection_prompts_smoke():
    """reflection 两 builder 渲染（回归护栏）。"""
    evidence = [{"tool": "echo", "params": {"text": "q"}, "result": "r", "success": True}]
    draft = {"summary": "s", "conclusions": [], "next_steps": []}
    crit = PromptManager.build_reflection_critique_prompt(evidence, draft)
    refine = PromptManager.build_reflection_refine_prompt(evidence, draft, [])
    assert "echo" in crit
    assert "s" in refine


def test_reflection_critique_compacts_evidence_without_mutating_source():
    """超长证据优先保留被当前稿引用的后部记录、量测/时间锚点和省略计数。"""
    evidence = [
        {
            "tool": "yield_query",
            "params": {"batch": f"B{i:02d}"},
            "result": f"noise-{i} " * 40,
            "success": True,
            "error_code": None,
        }
        for i in range(12)
    ]
    evidence[-1]["result"] += "2026-09-12 10:30 yield=81.2%"
    draft = {
        "summary": "批次 B11 良率下降",
        "conclusions": [
            {
                "claim": "B11 良率为 81.2%",
                "supporting_evidence": ["yield_query: batch=B11"],
                "confidence": 0.9,
            }
        ],
        "next_steps": [],
        "explicit_abstention": [],
    }
    before_evidence = deepcopy(evidence)
    before_draft = deepcopy(draft)

    prompt = PromptManager.build_reflection_critique_prompt(
        evidence,
        draft,
        max_tokens=2600,
        count_tokens=len,
    )

    assert len(prompt) <= 2600
    assert "E0012" in prompt
    assert "B11" in prompt
    assert "2026-09-12 10:30" in prompt
    assert "81.2%" in prompt
    assert '<omitted section="evidence"' in prompt
    omitted = int(re.search(r'<omitted section="evidence" records="(\d+)"', prompt).group(1))
    assert omitted == len(evidence) - prompt.count("[E")
    assert evidence == before_evidence
    assert draft == before_draft


def test_reflection_critique_compacts_draft_semantically():
    """稿件缩减保留结论、证据引用、置信度和显式放弃，省略低优先内容。"""
    evidence = [
        {
            "tool": "yield_query",
            "params": {"batch": "B11"},
            "result": "yield=81.2%",
            "success": True,
        }
    ]
    draft = {
        "summary": "良率下降" + "摘要" * 300,
        "conclusions": [
            {
                "claim": "B11 良率为 81.2%" + "结论" * 150,
                "supporting_evidence": ["yield_query: batch=B11"],
                "confidence": 0.88,
            }
        ],
        "next_steps": [f"低优先下一步 {i} " + "内容" * 100 for i in range(20)],
        "assumptions": ["长假设" * 200],
        "explicit_abstention": ["缺少设备日志，暂不判断设备根因"],
    }
    before = deepcopy(draft)

    prompt = PromptManager.build_reflection_critique_prompt(
        evidence,
        draft,
        max_tokens=2600,
        count_tokens=len,
    )

    assert len(prompt) <= 2600
    assert "B11 良率为 81.2%" in prompt
    assert "yield_query: batch=B11" in prompt
    assert "0.88" in prompt
    assert "缺少设备日志" in prompt
    assert '<omitted section="draft"' in prompt
    assert draft == before


def test_reflection_refine_prioritizes_critical_and_recent_issues():
    """超长 issues 先保留 critical，再保留较新的 minor，并标出省略数量。"""
    evidence = [{"tool": "echo", "params": {"q": "x"}, "result": "ok", "success": True}]
    draft = {"summary": "s", "conclusions": [], "next_steps": [], "explicit_abstention": []}
    issues = [
        {
            "severity": "minor",
            "dimension": "completeness",
            "description": f"旧 minor {i} " + "说明" * 100,
        }
        for i in range(8)
    ]
    issues.append(
        {
            "severity": "critical",
            "dimension": "grounding",
            "claim": "关键结论",
            "description": "关键证据缺失" + "详情" * 100,
        }
    )
    issues.append(
        {
            "severity": "minor",
            "dimension": "next_steps_actionable",
            "description": "最新 minor 应优先于旧 minor" + "详情" * 100,
        }
    )
    before = deepcopy(issues)

    prompt = PromptManager.build_reflection_refine_prompt(
        evidence,
        draft,
        issues,
        max_tokens=2200,
        count_tokens=len,
    )

    assert len(prompt) <= 2200
    assert "关键证据缺失" in prompt
    assert "最新 minor 应优先于旧 minor" in prompt
    assert '<omitted section="issues"' in prompt
    omitted = int(re.search(r'<omitted section="issues" items="(\d+)"', prompt).group(1))
    assert 0 < omitted < len(issues)
    assert issues == before


def test_build_system_prompt_smoke():
    """system builder 冒烟（带/不带工具描述）。"""
    base = PromptManager.build_system_prompt()
    with_tools = PromptManager.build_system_prompt("tool_a: 描述")
    assert base
    assert "tool_a" in with_tools
