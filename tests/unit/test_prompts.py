"""
PromptManager 提示词构建单元测试

覆盖（补 Slice 1 缺口）：planning 三 builder 渲染 + 关键约束子串；reflection 两 builder
冒烟；system builder 冒烟。builder 是纯文本渲染，断言关键内容与占位插值正确。
"""

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


def test_build_system_prompt_smoke():
    """system builder 冒烟（带/不带工具描述）。"""
    base = PromptManager.build_system_prompt()
    with_tools = PromptManager.build_system_prompt("tool_a: 描述")
    assert base
    assert "tool_a" in with_tools
