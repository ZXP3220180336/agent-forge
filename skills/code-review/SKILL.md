---
name: code-review
description: 审查代码或变更的正确性、结构和回归风险，按工程 Gate 输出有证据的缺陷；专项重试和生命周期检查按需转入对应 Skill。
---

# 代码审查

读取 [等级与 Gate](../../docs/engineering/ai-engineering-rules.md#levels)，按[通用评审维度](../../docs/engineering/ai-engineering-rules.md#review-criteria)审查受影响路径及必要调用方，不把偏好当作缺陷。

1. 确认审查基准和目标 diff，理解预期行为、公共契约、相关测试。分清本次引入的问题与无关历史债务。
2. 先检查适用 G0，再验证命中 G1 的结论和证据；G2 只触发结构分析，G3 仅在有实际收益时提出建议。
3. 自动重复路径使用 [retry-review](../retry-review/SKILL.md)；Agent 调用、usage、Guard 或终态路径使用 [agent-lifecycle-review](../agent-lifecycle-review/SKILL.md)。同一缺陷合并报告。
4. 对可疑问题追踪具体触发输入、状态转换与可观察影响，必要时运行针对性测试。缺少证据的风险标为待验证，不断言已发生。
5. 输出可行动发现；审查本身不授权修改代码或对外发布评论。

每条发现包含：严重度、文件与准确位置、触发条件、后果、证据、最小修复方向，可附规则 ID。P0 为已证实的紧急严重问题，P1 为应优先修复的高影响问题，P2 为一般缺陷，P3 为低影响问题。G0 不自动等于 P0；按可达性和实际影响分级。

先报告阻断问题，再列待验证项。没有发现时明确说明“未发现可证实问题”，同时说明审查范围和验证限制，不据此保证全库无 Bug。行数、命名偏好和没有新增类不能单独构成阻断。


## 项目接入

按 [项目边界](../../docs/project/architecture.md)审查层次/字段契约；对照 [冲突裁决](../../adr/2026-09-12-single-source-governance.md)区分现行限定行为和历史建议，不能将旧 ADR 的已替代条款作为缺陷依据。
