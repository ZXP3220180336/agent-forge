# 领域层 Reasoning 模块问题追踪

> **用途**：登记 Domain 层 Reasoning 模块（`app/domain/reasoning/`）的问题记录（发现 → 分析 → 修复 → 验证 → 教训）。
> **更新日期**：2026-08-30
> **关联**：[推理策略说明文档](../../../docs/domain_doc/reasoning_doc/reasoning.md) · [ADR context-budget](../../../adr/domain/reasoning/2026-08-28-context-budget.md)

## 状态图例

| 状态 | 含义 |
| --- | --- |
| ✅ 已修复 | 问题已修复并验证 |
| 🔶 分析中 | 根因分析中，未修复 |
| ⬜ 待处理 | 已发现，未分析 |

## 问题索引

| ID | 问题 | 状态 | 涉及模块 | 日期 |
| --- | --- | --- | --- | --- |
| [REASON-001](2026-08-30-context-budget-placement.md) | 上下文预算仅工具路径生效，非工具重试路径漏裁 | ✅ 已修复 | reasoning/react | 2026-08-30 |
| [REASON-002](2026-08-30-unknown-partial-progress.md) | 未捕获异常（UNKNOWN）路径不保留部分进度，与其余终结护栏不一致 | ✅ 已修复 | reasoning/react | 2026-08-30 |
| [REASON-003](2026-08-30-cancel-event-semantics.md) | 取消信号语义错位 + 未接线：优雅取消被误判为 LLM 失败 / 无调用方 | ✅ 已修复 | reasoning/react · agent/executor · task · chat | 2026-08-30 |
| [REASON-004](2026-08-30-protocol-error-empty-tool-calls.md) | finish_reason=tool_calls 信号与数据/工具可用性不一致（空列表 / 无工具）→ 协议异常短路 | ✅ 已修复 | reasoning/react | 2026-08-30 |
| [REASON-005](2026-08-30-unknown-error-redaction.md) | UNKNOWN error 拼接完整异常文本：内部细节（路径/敏感值）泄漏到产品侧 | ✅ 已修复 | reasoning/react | 2026-08-30 |
| [REASON-007](2026-08-31-llm-fail-retry-limit.md) | LLM 失败重试无独立上限：handler CONTINUE 可无限重试烧钱 | ✅ 已修复 | reasoning/react · agent · settings | 2026-08-31 |
| [REASON-008](2026-08-31-empty-output-blank-assistant.md) | 空输出重试轮向历史追加空 assistant 消息：累积污染上下文 | ✅ 已修复 | reasoning/react | 2026-08-31 |

## 新问题登记规范

1. **命名**：`<日期>-<短横线描述>.md`——日期为问题确立日（`YYYY-MM-DD`），描述为该问题的短 slug
2. **编号（索引 ID）**：REASON-XXX 递增，仅用于索引表展示；**文件名不含编号**
3. **模板**：元信息块（状态/优先级/来源/涉及模块）→ 问题描述（现象/影响/根因）→ 工业级参照 → 修复方案（含决策取舍）→ 实施记录 → 验证 → 教训沉淀
4. **登记**：新建文件后同步更新上方索引表
