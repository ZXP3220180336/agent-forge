# 工具安全与审计（RiskLevel / ToolAuditor / ApprovalGate）说明文档

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
3. **审批骨架落地（默认放行）**：`requires_approval` 工具经 `ApprovalGate` 确认，默认 `AutoApprovalGate` 放行；未来接真实审批（API 确认 / 管理端审批 / 策略引擎）仅换注入实现

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

`record()` 落一条结构化事件到 `app.events` logger（`event_name="tool_call"`），字段：`tool` / `risk_level` / `category` / `success` / `elapsed` / `retry_count` / `params` / `error` / `error_code` / `content`。

- `params` 截断至 `params_max_chars`（默认 1000，防 writeFile 把整文件内容写进日志）
- **敏感键掩码**：`params` 序列化前对 `api_key` / `token` / `secret` / `password` / `authorization` / `credential` 键值掩码为 `***`（词边界匹配，覆盖驼峰 / passwd / 复数变体，嵌套 dict / list 递归，防凭据落盘）
- **error / content 文本级掩码**：自由文本无键结构，按敏感模式兜底掩码（`key=值` 键值对保留分隔符 / `Bearer xxx` / `sk-xxx` 前缀），防错误信息与页面内容经审计日志泄露凭据
- `content` 预览截断至 `content_preview_chars`（默认 200）
- `enabled=True` 默认常开（不设 settings 开关，防静默关闭安全审计；`enabled` 参数供测试注入）

### 人工审批通道（ApprovalGate）

`BaseTool.requires_approval` 声明「需人工审批」；executor 在参数校验通过后、执行前经 `ApprovalGate.request(tool_name, parameters)` 征得确认：

- 返回 `True` → 放行执行；返回 `False` → 拒绝（返回 `"工具调用被拒绝：等待人工审批"`，工具不执行）
- 默认 `AutoApprovalGate` 一律放行（保持「不拦截」行为）；未来接真实审批（API 确认 / 管理端审批 / 策略引擎）仅实现 `ApprovalGate` 并在 `ToolService(approval_gate=...)` 注入，Agent 层零改动
- 审批拒绝路径同样审计留痕（覆盖全路径）

### SSRF 防护（共享）

`security.py` 提供 URL 主机校验（`web_browse` / `http_api` 共享，经 httpx `event_hooks["request"]` 注入每个请求含重定向跳）：

- `check_host_sync` / `check_host_async`：裸 IP 拒绝（保守策略含公网）→ 内网保留后缀（`.internal` / `.local` / `.corp` 等）拒绝 → `getaddrinfo` 解析后命中内网 · 环回 · 链路本地 · 保留 · 未指定 · 组播 · **非公网站段**拒绝（`not ip.is_global` 兜底拦截 CGNAT 100.64.0.0/10 等 `is_*` 全 False 的绕过网段；DNS rebinding 为解析层缓解，连接层无法复核；DNS 异步版经 `asyncio.to_thread` 不阻塞）
- `ssrf_on_request(request)`：httpx 请求钩子，请求前校验 `request.url.host`，命中抛 `SSRFError` 中断请求
- `SSRFError`：业务拦截异常，工具捕获归因返回（如 `"请求被安全策略拦截（SSRF 防护）"`）

用途：`web_browse` / `http_api` 防护 URL 直达内网 / 云元数据（`169.254.169.254`）。

## 对外接口

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `ToolAuditor.record` | `async (*, tool_name, risk_level, category, success, elapsed, parameters, error=None, error_code=None, retry_count=0, content_preview="")` | 记录一条审计事件 |
| `ApprovalGate.request` | `async (tool_name: str, parameters: dict) -> bool` | 审批通道协议：执行前确认，返回 False 拒绝 |
| `AutoApprovalGate.request` | `async (tool_name: str, parameters: dict) -> bool` | 默认审批通道：恒 True 放行 |
| `check_host_sync` / `check_host_async` | `(hostname: str) -> None` | SSRF 主机校验：裸 IP / 内网 TLD / 解析到内网站段抛 `SSRFError` |
| `ssrf_on_request` | `async (request) -> None` | httpx 请求钩子：每跳校验 host，命中抛 `SSRFError` |

调用方：`executor._audit`（每个 execute 一条最终结果，含未注册工具的兜底元数据）；`executor` 步骤 5 经 `ApprovalGate` 确认 `requires_approval` 工具。

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
4. **密钥脱敏**：已实现（见上方「ToolAuditor 审计」敏感键掩码）——`params` 序列化前掩码敏感键值，params 截断之外再降凭据泄露面
5. **`requires_approval`**：由 executor 经 `ApprovalGate` 确认，默认 `AutoApprovalGate` 放行；接入真实审批前勿误以为已拦截

## 测试状态

`tests/unit/test_tool_audit.py`（13 用例）：RiskLevel 排序 / record 字段完整性 / L2→WARNING、L0→INFO / enabled=False 静默 / params 截断 / error_code 字段 / error_code 默认 None / 敏感键掩码（params 脱敏 / 审计日志脱敏 / error 文本脱敏 / content Bearer 与 sk- 脱敏）。
`tests/unit/test_tool_approval.py`（5 用例）：审批通道——默认放行 / 拒绝拦截 / 不触发 / 审计留痕。

## 设计决策

- L0-L3 分级 + 审计留痕 + 不拦截执行 → [ADR](../../../adr/integration/tools/2026-08-17-risk-levels-audit-no-enforcement.md)

## 相关文档

- [工具模块接口文档](tools.md)（BaseTool.risk_level / requires_approval 契约）
- [ToolService 执行流程](../../../app/integration/tools/executor.py)（审计接入点）
- [TOOLS-011 问题记录](../../../issues/integration/tools/2026-08-19-http-api-approval-ssrf.md)（SSRF 共享防护抽取 + http_api 审批）
- [TOOLS-016 问题记录](../../../issues/integration/tools/2026-08-19-audit-sensitive-key-masking.md)（审计参数敏感键掩码）
