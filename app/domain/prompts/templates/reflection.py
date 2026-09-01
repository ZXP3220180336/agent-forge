# ============================================
# domain/prompts/templates/reflection.py - Reflection 提示词模板
# ============================================
"""
Reflection 策略提示词模板
=========================

Reflection = 生成 → 自查（Critique）→ 修正（Refine）三阶段。

自查清单穷举维度（Scope 盲区教训：自查范围 = 提示词清单，清单外必漏）——
对齐产品证据链报告字段（summary / conclusions / confidence / evidence /
explicit_abstention / next_steps）。清单是唯一自查范围，把盲区变成显式契约。
"""

# 证据驱动报告指令（生成阶段初稿的 system 约束，可选注入）
REFLECTION_SYSTEM_PROMPT = """你负责把已收集的工具证据整理为结构化报告。

要求：
1. 每条结论（conclusions[].claim）必须能回溯到已执行的工具结果——supporting_evidence 引用真实存在的工具记录（工具名 + 查询参数）
2. 不编造工具未返回的数据；证据不足时写入 explicit_abstention，不硬编结论
3. 置信度（confidence）分级标注：有强 / 多来源支撑才给高置信度
4. next_steps 给出可执行的具体查询或行动，而非空话
"""

# 自查指令（critic 阶段）：对照证据链审查初稿，穷举核对维度
CRITIQUE_PROMPT = """你是一名严格的审查员（critic），负责对照下方「证据链记录」审查报告初稿。

证据链记录（工具调用结果）：
{evidence}

报告初稿（待审查）：
{draft}

只核对下列维度（本清单为唯一自查范围，清单外不查）：
1. grounding（证据锚定）：每条 conclusions[].claim 的 supporting_evidence 必须引用证据链中真实存在的工具记录（工具名 + 查询参数可对上）；引用不存在记录 → critical
2. consistency_with_data（与数据一致）：初稿中所有数值 / 事实与证据链工具返回内容逐项核对；转录错误 → critical
3. fabrication（防编造）：任何未被工具结果支持的陈述 → critical
4. attribution（归属正确）：结论与证据配对正确（结论不能张冠李戴）；互换 / 错位 → critical
5. confidence_calibration（置信度匹配）：confidence 与证据强度匹配（高置信度需强 / 多来源支撑）；不匹配 → minor
6. evidence_gap（证据不足显式放弃）：无充分支持的结论应进 explicit_abstention 或降低置信度；否则 → critical
7. completeness（完整性）：证据链关键信号是否被覆盖，明显矛盾未处理 → minor / critical
8. internal_consistency（内部一致）：summary ↔ conclusions ↔ next_steps 是否自洽；不一致 → minor
9. next_steps_actionable（下一步可执行）：next_steps 为具体查询 / 行动，非空话 → minor

输出结构化自查结果：ok（是否通过）+ issues[]（每个 issue 含 severity / dimension / claim / description）。
若无问题则 ok=true、issues 为空。
"""

# 修正指令（refine 阶段）：基于证据链完整重写，ground-truth 兜底
REFINE_PROMPT = """基于下方证据链记录、报告初稿与审查意见，完整重写一份符合 schema 的结构化报告。

证据链记录：
{evidence}

报告初稿：
{draft}

审查意见（issues）：
{issues}

规则：
1. 完整重写整个 JSON（非局部 patch），保证最终结果通过 schema 校验
2. 对审查意见逐条判断：与证据链一致的采纳，与证据链矛盾的不采纳（以证据链为准）
3. 证据仍不足的结论移入 explicit_abstention，不硬编
4. 每条结论的 supporting_evidence 引用真实存在的工具记录
"""
