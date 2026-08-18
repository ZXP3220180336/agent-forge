# 工具模块决策记录（ADR）

> **用途**：登记 Integration 层工具模块（`app/integration/tools/`）的结构性/契约性设计决策，记录 Context → Decision → Consequences 完整前因后果，供追溯与复用。
> **更新日期**：2026-08-17
> **关联**：[工具模块接口文档](../../../docs/integration_doc/tools_doc/tools.md) · [问题追踪](../../../issues/domain/agent/README.md)

## 状态图例

| 状态 | 含义 |
| --- | --- |
| ✅ 已采纳 | 决策已实施，当前生效 |
| 🔶 已替代 | 被后续决策替代（在替代决策文件内说明） |
| ⬜ 已放弃 | 评估后不采纳（附理由） |

## 决策索引

| ID | 决策 | 状态 | 涉及模块 | 决策日期 |
| --- | --- | --- | --- | --- |
| [TOOLS-ADR-001](2026-08-17-six-component-alignment.md) | 六大子组件对齐 + 选择器全量注入 + head+tail 截断 | ✅ 已采纳 | tools 全模块 | 2026-08-17 |
| [TOOLS-ADR-002](2026-08-17-jsonschema-strict-validation.md) | 参数校验：jsonschema 严格校验 + 错误归因 | ✅ 已采纳 | validator / base | 2026-08-17 |
| [TOOLS-ADR-003](2026-08-17-risk-levels-audit-no-enforcement.md) | 风险分级 L0-L3 + 审计留痕（不拦截） | ✅ 已采纳 | security / executor | 2026-08-17 |
| [TOOLS-ADR-004](2026-08-17-tool-timeout-priority.md) | 工具自声明默认超时（调用方 > 工具 > 全局） | ✅ 已采纳 | base / executor / builtin | 2026-08-17 |
| [TOOLS-ADR-005](2026-08-17-external-tool-hot-reload.md) | 外部工具热加载（内嵌式可信插件档 + 分层对齐） | ✅ 已采纳 | loader / base / executor / tool_service | 2026-08-17 |

## 新决策登记规范

1. **命名**：`<日期>-<短横线描述>.md`——日期为决策确立日（`YYYY-MM-DD`），描述为该决策的短 slug
2. **编号（索引 ID）**：TOOLS-ADR-XXX 递增（001、002、…），仅用于索引表展示；**文件名不含编号**
3. **模板**：元信息块（状态/日期/涉及模块/关联文档）→ Context（背景与动机，含备选方案）→ Decision（决策内容，含取舍依据）→ Consequences（正面/负面后果）
4. **登记**：新建文件后同步更新上方索引表

## 维护原则

- **一个决策一个文件**：跨模块决策以主决策模块归位
- **与 issues 分离**：本目录沉淀「为什么做这个决策」（可复用的设计理由）；issues 沉淀「问题从发现到验证的生命周期」。问题驱动的决策以问题记录为主、决策作为修复方案的一部分，不重复建 ADR
- **目录与 app/ 结构对齐**：`adr/<层名>/<模块名>/`（本目录 = `adr/integration/tools/` 对应 `app/integration/tools/`）
