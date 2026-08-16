# LLM 模块决策记录（ADR）

> **用途**：登记 Integration 层 LLM 模块（`app/integration/llm/`）的结构性/契约性设计决策，记录 Context → Decision → Consequences 完整前因后果，供追溯与复用。
> **更新日期**：2026-08-16
> **关联**：[LLM 层说明文档](../../../docs/integration_doc/llm_doc/llm.md) · [问题追踪](../../../issues/integration/llm/README.md)

## 状态图例

| 状态 | 含义 |
| --- | --- |
| ✅ 已采纳 | 决策已实施，当前生效 |
| 🔶 已替代 | 被后续决策替代（在替代决策文件内说明） |
| ⬜ 已放弃 | 评估后不采纳（附理由） |

## 决策索引

| ID | 决策 | 状态 | 涉及模块 | 决策日期 |
| --- | --- | --- | --- | --- |
| [LLM-ADR-001](2026-08-07-unified-structured-entry-degradation.md) | 统一结构化输出入口 + 三级降级策略 | ✅ 已采纳 | llm_service / structured | 2026-08-07 |
| [LLM-ADR-002](2026-08-15-pricing-prefix-match-fixed-table.md) | 定价查找：最长前缀匹配 + 模块内固定定价表 | ✅ 已采纳 | cost_tracker | 2026-08-15 |
| [LLM-ADR-003](2026-08-01-streaming-parse-pure-function.md) | 流式解析策略：纯函数无状态 + tool_call 延迟组装 + usage 独立提取 | ✅ 已采纳 | streaming | 2026-08-01 |
| [LLM-ADR-004](2026-08-01-client-pool-lazy-close-tracking.md) | 连接池管理：懒加载 + 主动关闭 + 热切换关闭追踪 | ✅ 已采纳 | client | 2026-08-01 |
| [LLM-ADR-005](2026-08-01-streaming-rectification-retry.md) | 流式整流重试策略：首 token 前中断自动恢复 | ✅ 已采纳 | streaming_rectifier / llm_service | 2026-08-01 |
| [LLM-ADR-006](2026-08-01-retry-circuit-breaker-architecture.md) | 重试与熔断架构：CircuitBreaker + 指数退避 + 抖动 + fallback | ✅ 已采纳 | retry / llm_service | 2026-08-01 |
| [LLM-ADR-007](2026-08-01-circuit-breaker-window-semantics.md) | 熔断窗口语义与请求级记账（RETRYABLE 计入 / fallback 隔离） | ✅ 已采纳 | retry | 2026-08-01 |

## 新决策登记规范

1. **命名**：`<日期>-<短横线描述>.md`——日期为决策确立日（`YYYY-MM-DD`），描述为该决策的短 slug（对齐 CLAUDE.md 规则「决策文件名使用 <日期-决策> 方式命名」）。
2. **编号（索引 ID）**：LLM-ADR-XXX 递增（001、002、…），仅用于索引表展示；**文件名不含编号**。
3. **模板**：复制既有决策文件的结构——元信息块（状态/日期/涉及模块）→ Context（背景与动机，含备选方案）→ Decision（决策内容，含取舍依据）→ Consequences（正面/负面后果）。
4. **登记**：新建文件后同步更新上方索引表（ID / 决策标题 / 状态 / 涉及模块 / 决策日期）。
5. **变更**：决策被替代时，更新原文件状态为 🔶 已替代，并在新决策文件内引用旧文件。

## 维护原则

- **一个决策一个文件**：跨模块决策以主决策模块归位（如限流形态归 limiter 模块）。
- **与 issues 分离**：本目录沉淀「为什么做这个决策」（可复用的设计理由）；issues 沉淀「问题从发现到验证的生命周期」（可回溯的缺陷记录）。问题驱动的决策（问题 → 调研 → 修复）以问题记录为主、决策作为修复方案的一部分（见 [问题文档](../../../issues/integration/llm/README.md) 的「修复方案（含决策取舍）」），不重复建 ADR。
- **目录与 app/ 结构对齐**：`adr/<层名>/<模块名>/`（本目录 = `adr/integration/llm/` 对应 `app/integration/llm/`）。
