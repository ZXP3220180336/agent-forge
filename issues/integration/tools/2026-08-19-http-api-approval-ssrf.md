# TOOLS-011 external http_api 写操作未声明审批 + 无 SSRF 防护

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（安全边界）
> **来源**：2026-08-18 工具模块代码审核（external 组 · 重要项 5）
> **涉及模块**：`app/integration/tools/external/http_api.py`（HttpApiTool）+ `app/integration/tools/security.py`（SSRF 共享防护抽取）+ `builtin/web_browse.py`（复用调整）
> **关联文档**：[external.md](../../../docs/integration_doc/tools_doc/external.md) · [security.md](../../../docs/integration_doc/tools_doc/security.md)

---

## 问题描述

### 现象

- `http_api` 刻意覆盖 `L1_WRITE` 演示写能力（POST/PUT/DELETE 改外部状态），但 `requires_approval` 保持默认 `False`，配合默认放行审批——Agent 可被诱导向内网 / 云元数据服务发起**改状态**请求；
- URL 无主机 / 网络约束（任意 URL + `follow_redirects`），与已修复的 web_browse 同源 SSRF 风险，且写工具风险更高。

### 影响

SSRF + 写副作用叠加：Agent 可被网页内容 / 提示注入驱动，向 `169.254.169.254` 云元数据或内网服务发起 POST/PUT/DELETE 等破坏性请求。

### 根因

写能力工具未声明审批需求；URL 校验缺位（web_browse 的 SSRF 防护未共享到 http_api）。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 写操作 HITL（人工审批） | 不可逆 / 副作用操作须人工确认（`requires_approval` 声明 + ApprovalGate） |
| SSRF 防护（web_browse 已落地） | 裸 IP / 内网 TLD / 解析到内网站段拒绝——共享抽取避免重复实现 |

**核心**：写能力工具声明审批；SSRF 防护共享（安全横切一处维护）。

---

## 修复方案（含决策取舍）

**决策**：

| 改动 | 内容 |
| --- | --- |
| `security.py` | SSRF 防护从 web_browse **抽取为共享模块**：`check_host_sync` / `check_host_async` / `ssrf_on_request` / `SSRFError`（逻辑不变，公开名供跨模块 import） |
| `http_api.py` | `requires_approval = True`（写能力工具一律审批）；client 注入 `event_hooks["request"]=[ssrf_on_request]`；execute 捕获 `SSRFError` 归因；description 明确「仅 http/https、裸 IP / 内网 / 云元数据被拒、生产需白名单 + 审批」 |
| `web_browse.py` | 删除内联 SSRF 块，改 import security 共享实现（行为不变） |

**取舍理由**：

1. **工具级审批而非方法级**：`requires_approval` 是工具级元数据（executor 在 execute 前检查，参数未知）——写能力工具一律审批是最小且安全的声明；GET 读也审批的代价可接受（http_api 定位是外部写操作工具）；
2. **SSRF 共享抽取**：web_browse 已有完整实现，抽取到 security.py（安全横切）一处维护，http_api 复用——DRY + 规则统一；
3. **向后兼容**：web_browse 行为不变（仅换实现来源），既有 SSRF 测试改 import 路径后全通过。

**语义边界**：默认 `AutoApprovalGate` 放行（审批声明不拦截实际执行），生产接真实 ApprovalGate 才真正拦截；SSRF 拦截在 `execute` 请求前（不发起连接）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/security.py` | SSRF 防护块（`_PRIVATE_TLD_SUFFIXES` / `SSRFError` / `_is_blocked_ip` / `check_host_sync` / `check_host_async` / `ssrf_on_request`）+ TYPE_CHECKING httpx | `tests/unit/test_web_browse_ssrf.py` import 改 `from security import SSRFError, check_host_sync`（原 web_browse），11 用例全通过 |
| `app/integration/tools/builtin/web_browse.py` | 删除内联 SSRF 块；import `SSRFError, ssrf_on_request` from security；client event_hooks 复用 | `test_tool_execution.py` execute 级 SSRF 拦截用例通过 |
| `app/integration/tools/external/http_api.py` | `requires_approval=True`；client 注入 `ssrf_on_request`；execute 捕获 `SSRFError`；description 明确 URL 限制 | `tests/unit/test_http_api_tool.py` 新增 `test_ssrf_blocks_internal_url`（裸 IP 拦截）+ metadata 断言补 `requires_approval is True` |
| 文档 | [security.md](../../../docs/integration_doc/tools_doc/security.md)（SSRF 共享节 + 对外接口）；[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)（web_browse SSRF 指向 security）；[external.md](../../../docs/integration_doc/tools_doc/external.md)（示例工具描述补审批 + SSRF） | — |

---

## 验证

- 相关测试 **57 passed**（SSRF 抽取 + http_api + 工具执行集成）
- 全量测试待提交前确认（增量改动：SSRF 共享 + http_api 声明，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **写能力工具必须声明审批**：`L1_WRITE` 风险标注 + `requires_approval=True` 是配套声明——只标风险不声明审批，安全边界不完整。
- **安全横切一处维护**：SSRF 防护抽到 security.py 共享（web_browse / http_api 复用），规则演进只改一处——安全逻辑不能在各工具重复实现漂移。
- **防护 + 声明 + 描述三件套**：http_api 同时落地 SSRF 拦截（强制）、`requires_approval`（声明）、description 明确 URL 限制（告知 LLM）——强制防护 + 声明式约束 + 语义告知缺一不可。
