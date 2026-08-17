# 工具安全与审计（RiskLevel / ToolAuditor）说明文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/security.py`
> **职责**：工具风险分级标注 + 执行审计留痕（结构化日志）
> **状态**：✅ 已实现
> **工业级对照**：L0-L3 风险分级（FreeBuf / CSDN 工业共识）+ 全量审计（工具名 / 参数 / 结果 / 耗时 / 风险级）

---

## 📋 目录

- [设计目标](#设计目标)
- [核心概念解释](#核心概念解释)
- [对外接口](#对外接口)
- [与 ExecutionHooks 的分工](#与-executionhooks-的分工)
- [边界情况](#边界情况)
- [测试状态](#测试状态)
- [设计决策](#设计决策)
- [相关文档](#相关文档)

---

## 设计目标

1. **分级标注**：每个工具声明 `RiskLevel`（L0 只读 / L1 写 / L2 危险 / L3 禁用），为未来人工确认 / 沙箱拦截预留元数据
2. **审计留痕**：每次工具调用记录一条结构化事件（`tool_call`），覆盖成功 / 失败 / 未注册 / 校验失败 / 超时全路径
3. **不拦截执行**：当前单用户本地运行，分级只标注 + 审计，不做执行拦截（高危拦截列为未来增强）

## 核心概念解释

### RiskLevel 分级

| 级别 | 值 | 含义 | 本项目工具 |
| --- | --- | --- | --- |
| `L0_READONLY` | 0 | 只读，无破坏性 | search / readFile / web_browse |
| `L1_WRITE` | 1 | 可修改数据，影响可控 | writeFile |
| `L2_DANGEROUS` | 2 | 潜在不可逆影响 | code_exec（另有黑名单业务拦截） |
| `L3_DISABLED` | 3 | 禁用（预留：风险过高禁止注册） | — |

`IntEnum` 可排序（`L2 > L1`），便于「是否 ≥ 危险级」判断。日志级别按分级映射：L0/L1→INFO、L2→WARNING、L3→ERROR，方便 ops 按级别检索。

### ToolAuditor 审计

`record()` 落一条结构化事件到 `app.events` logger（`event_name="tool_call"`），字段：`tool` / `risk_level` / `category` / `success` / `elapsed` / `retry_count` / `params` / `error` / `content`。

- `params` 截断至 `params_max_chars`（默认 1000，防 writeFile 把整文件内容写进日志）
- `content` 预览截断至 `content_preview_chars`（默认 200）
- `enabled=True` 默认常开（不设 settings 开关，防静默关闭安全审计；`enabled` 参数供测试注入）

### 人工审批通道（ApprovalGate）

`BaseTool.requires_approval` 声明「需人工审批」；executor 在参数校验通过后、执行前经 `ApprovalGate.request(name, parameters)` 征得确认：

- 返回 `True` → 放行执行；返回 `False` → 拒绝（返回 `"工具调用被拒绝：等待人工审批"`，工具不执行）
- 默认 `AutoApprovalGate` 一律放行（保持「不拦截」行为）；未来接真实审批（API 确认 / 管理端审批 / 策略引擎）仅实现 `ApprovalGate` 并在 `ToolService(approval_gate=...)` 注入，Agent 层零改动
- 审批拒绝路径同样审计留痕（覆盖全路径）

## 对外接口

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `ToolAuditor.record` | `async (*, tool_name, risk_level, category, success, elapsed, parameters, error=None, retry_count=0, content_preview="")` | 记录一条审计事件 |

调用方：`executor._audit`（每个 execute 一条最终结果，含未注册工具的兜底元数据）。

## 与 ExecutionHooks 的分工

| 维度 | ExecutionHooks | ToolAuditor |
| --- | --- | --- |
| 定位 | 可扩展通知机制（用户可注册任意钩子） | 系统级强制留痕 |
| 触发 | 仅成功路径 | 成功 / 失败 / 未注册 / 校验失败 / 超时全路径 |
| 故障语义 | 钩子异常仅 warning | 审计失败不阻断执行（日志尽力而为） |

两者独立，不互相复用。

## 边界情况

1. **未注册工具**：审计兜底 `tool_name` 保留原始名、`risk_level=L0_READONLY`、`category="unknown"`
2. **参数非 dict**（JSON 解析失败的原始字符串）：审计包装为 `{"raw": str}`，截断 500 字符
3. **审计失败**：`log_event_async` 异常不影响工具执行（尽力而为）
4. **密钥脱敏**：列为未来增强（params 截断已降低泄露面）
5. **`requires_approval`**：由 executor 经 `ApprovalGate` 确认，默认 `AutoApprovalGate` 放行；接入真实审批前勿误以为已拦截

## 测试状态

`tests/unit/test_tool_audit.py`（6 用例）：RiskLevel 排序 / record 字段完整性 / L2→WARNING、L0→INFO / enabled=False 静默 / params 截断。
`tests/unit/test_tool_approval.py`（5 用例）：审批通道——默认放行 / 拒绝拦截 / 不触发 / 审计留痕。

## 设计决策

- L0-L3 分级 + 审计留痕 + 不拦截执行 → [ADR](../../../adr/integration/tools/2026-08-17-risk-levels-audit-no-enforcement.md)

## 相关文档

- [工具模块接口文档](tools.md)（BaseTool.risk_level / requires_approval 契约）
- [ToolService 执行流程](../../../app/integration/tools/executor.py)（审计接入点）
