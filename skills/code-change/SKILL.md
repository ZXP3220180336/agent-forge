---
name: code-change
description: 实现项目功能、修复 Bug 或调整既有代码时，按变更触发工程 Gate，选择最小必要结构变化并完成验证。
---

# 代码变更

以 [通用规则](../../docs/engineering/ai-engineering-rules.md)为依据，按本次 Trigger 读取相关章节，不复制全部检查表。

1. 确认目标行为、现有契约和修改边界，读取直接相关代码、测试及项目局部规则。关键需求不明确时先给合理方案并澄清；不要猜测业务语义。
2. 识别命中的 E1–E9；非平凡任务记录简短计划及各文件职责，可合并重复 Gate。按项目工作流完成写前方案确认；架构、契约或范围发生实质变化时显式说明并确认。
3. 按现有职责、小范围方法提取、新对象的顺序选择设计。准备新增抽象时读取 Abstraction Gate，给出真实理由；不为行数指标重构。
4. Bug 先建立能失败的复现测试再修复。结构调整先保护旧行为，再实现新行为。无法运行测试时记录实际原因，不能伪造结果。
5. 按项目工作流完成调研与契约设计后实现，执行其单元测试及全量测试要求，并补充命中 Gate 的检查。涉及自动重试时读取 [retry-review](../retry-review/SKILL.md)；涉及 Agent 调用、资源或终态时读取 [agent-lifecycle-review](../agent-lifecycle-review/SKILL.md)，合并执行，不递归重复审查。
6. 核对 G0、相关边界、命中 Gate 与文档一致性；汇报改动结果、验证及未解决事项。简单修改无需完整架构报告。

推荐精简记录：`目标 → 命中 Gate → 结构决定及依据 → 验证证据 → 剩余限制`。这是一份结果摘要，不要求逐步推理。只有真实纠正带来可复用教训时才更新项目 lessons，不把每个任务变成新规则。


## 项目接入

按 [项目工作流](../../docs/engineering/project-workflow.md)维护 docs/todo.md、执行写前方案确认并核对实际测试配置；分层/契约变化加载 [项目边界](../../docs/project/architecture.md)。相关文档事实变化才更新，不机械复制。完成时更新计划与评审；真实纠正形成可复用教训才更新 lessons。

涉及文档事实变化时，按 [documentation-maintenance](../documentation-maintenance/SKILL.md)选择文档类型、更新唯一正文及索引，不另建来源副本。
