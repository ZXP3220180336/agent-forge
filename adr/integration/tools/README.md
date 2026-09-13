# 工具模块决策记录（ADR）

> **用途**：登记 Integration 层工具模块（`app/integration/tools/`）的结构性/契约性设计决策，记录 Context → Decision → Consequences 完整前因后果，供追溯与复用。
> **更新日期**：2026-09-13
> **关联**：[工具模块接口文档](../../../docs/integration_doc/tools_doc/tools.md) · [问题追踪](../../../issues/integration/tools/README.md)

## 决策索引

| ID | 决策 | 状态 | 涉及模块 | 决策日期 |
| --- | --- | --- | --- | --- |
| [TOOLS-ADR-001](2026-08-17-six-component-alignment.md) | 六大子组件对齐 + 选择器全量注入 + head+tail 截断 | ✅ 已采纳 | tools 全模块 | 2026-08-17 |
| [TOOLS-ADR-002](2026-08-17-jsonschema-strict-validation.md) | 参数校验：jsonschema 严格校验 + 错误归因 | ✅ 已采纳 | validator / base | 2026-08-17 |
| [TOOLS-ADR-003](2026-08-17-risk-levels-audit-no-enforcement.md) | 风险分级 L0-L3 + 审计留痕（不拦截） | ✅ 已采纳 | security / executor | 2026-08-17 |
| [TOOLS-ADR-004](2026-08-17-tool-timeout-priority.md) | 工具自声明默认超时（调用方 > 工具 > 全局） | ✅ 已采纳 | base / executor / builtin | 2026-08-17 |
| [TOOLS-ADR-005](2026-08-17-external-tool-hot-reload.md) | 外部工具热加载（内嵌式可信插件档 + 分层对齐） | ✅ 已采纳 | loader / base / executor / tool_service | 2026-08-17 |
| [TOOLS-ADR-006](2026-08-17-tool-lifecycle-paradigm.md) | 工具生命周期范式（有状态连接池 + 钩子 vs LangChain 无状态） | ✅ 已采纳 | base / tool_service / loader / web_browse / container | 2026-08-17 |
| [TOOLS-ADR-007](2026-08-17-tool-error-code.md) | 工具调用统一错误码（ErrorCode 6 码 + 审计聚合） | ✅ 已采纳 | tool_gateway / executor / security | 2026-08-17 |
| [TOOLS-ADR-008](2026-09-13-tool-execution-lifecycle.md) | 工具执行生命周期：终止、事实接管、资源冲突与恢复 | 方向已接受；实施细节待评审，代码待实施 | tools / ToolGateway / ReAct / 存储与装配 | 2026-09-13 |

登记、状态和维护规则见 [记录规范](../../../docs/engineering/documentation/records.md)。本表保留本模块的既有编号序列；历史状态为当时记录，不代表本轮重新验证。
