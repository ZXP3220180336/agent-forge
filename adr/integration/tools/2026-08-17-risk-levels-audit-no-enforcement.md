# 工具风险分级 + 审计留痕（不拦截执行）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-17
> **涉及模块**：`app/integration/tools/security.py`（RiskLevel / ToolAuditor）· `app/integration/tools/executor.py`（审计接入）
> **关联文档**：[security.md](../../../docs/integration_doc/tools_doc/security.md)

---

## Context

- 原工具系统安全仅靠 `code_exec` 硬编码危险命令黑名单（工具级业务拦截），无系统级风险分级与审计留痕
- 工业界共识（FreeBuf / CSDN / Lens Agents）：工具按 L0 只读 / L1 写 / L2 危险 / L3 禁用分级，高危工具强制人工确认或沙箱隔离；审计要求全量留痕（入参 / 结果 / 耗时 / 调用者）
- 当前项目为单用户本地运行，沙箱隔离（Docker / gVisor）与人工确认通道均超出当前需求，拦截价值低

## Decision

**引入 `RiskLevel`（IntEnum L0-L3）分级标注 + `ToolAuditor` 审计留痕到结构化日志（`tool_call` 事件），当前不做执行拦截。**

- **风险分级**：`L0_READONLY`（只读）/ `L1_WRITE`（写）/ `L2_DANGEROUS`（危险）/ `L3_DISABLED`（禁用预留）；IntEnum 可排序，便于「是否 ≥ 危险级」判断。内置工具：search / readFile / web_browse→L0，writeFile→L1，code_exec→L2
- **审计独立于 ExecutionHooks**：hooks 是可扩展通知（仅成功路径）；审计是系统级强制留痕，覆盖成功 / 失败 / 未注册 / 校验失败 / 超时全路径，每次 execute 记录 1 条最终结果
- **日志级别映射**：L0/L1→INFO、L2→WARNING、L3→ERROR，便于 ops 按级别检索
- **审计默认常开**：不设 settings 开关（防静默关闭安全审计）；`enabled` 构造参数供测试注入
- **参数 / 内容截断**：params 截断至 1000 字符（防 writeFile 把整文件内容写进日志）、content 预览 200 字符；密钥脱敏列为未来增强
- **不拦截 → 审批骨架落地（默认放行）**：`requires_approval` 字段由 executor 经 `ApprovalGate` 消费——默认 `AutoApprovalGate` 一律放行（保持不拦截行为），未来接真实审批（API 确认 / 管理端审批）仅换注入实现；code_exec 的黑名单为工具级业务拦截，与系统级审批机制互补

## Consequences

- **正面**：安全基线建立——每个工具声明风险级、每次调用留痕可追溯；L2 危险工具 WARNING 级别便于 ops 告警；未来加人工确认 / 沙箱拦截时，仅需消费已有 risk_level 元数据，架构零改动
- **负面**：审计日志有隐私面（params 进日志，靠截断兜底）与性能开销（每次调用一次日志写入）；分级为人工标注（新工具需自觉声明，默认 L0 可能低估风险）；审批骨架默认放行——`requires_approval` 工具当前未被真实拦截（拦截机制已就位，接入真实 ApprovalGate 后生效，本地单用户场景接受）
