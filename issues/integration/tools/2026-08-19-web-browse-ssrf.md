# TOOLS-003 web_browse SSRF：任意 URL + follow_redirects 无主机/网络约束

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（安全边界）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 重要项 4）
> **涉及模块**：`app/integration/tools/builtin/web_browse.py`（WebBrowseTool + 全局 httpx 单例）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`web_browse` 接受任意 URL（由 LLM 间接控制、可被网页内容提示注入），`httpx.AsyncClient` 无主机/网络约束，`follow_redirects=True` 进一步扩大攻击面。请求可直达内网 / 云元数据（`169.254.169.254`、应用自身 `127.0.0.1:8000` API），内网服务信息被提取进 LLM 上下文并经响应/日志外泄。

### 影响

SSRF 攻击面：内网探测、云元数据凭据窃取（IMDS）、绕过边界访问内部管理接口。web 工具是 RCA 主链路「查缺陷模式 / 查在线资料」的常用工具，风险直接作用于主链路。

### 根因

URL 主机不做任何校验即发起请求：裸 IP、内网域名、解析到内网站段的域名均未拦截；重定向跳转目标也未校验。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OWASP SSRF Prevention Cheat Sheet | 拒绝私网 / 环回 / 链路本地 / 保留网段；域名**解析后**校验 IP；重定向目标同样校验 |
| Python `ipaddress` 标准库 | `is_private / is_loopback / is_link_local / is_reserved / is_unspecified` 网段判定（IPv4-mapped IPv6 需归一） |
| 云元数据防护实践（IMDSv2） | `169.254.169.254` 属链路本地网段——链路本地即拦截 |

**核心**：裸 IP 拒绝（保守）+ 域名解析校验（防 DNS rebinding）+ 每跳重定向校验（防重定向逃逸）。

---

## 修复方案（含决策取舍）

**决策**：模块级 SSRF 防护函数 + httpx `event_hooks["request"]` 集成：

| 层 | 改动 |
| --- | --- |
| 校验函数 | `_check_host_sync`：裸 IP 一律拒绝（保守，仅允许域名访问）→ 内网保留后缀（`.local/.localhost/.internal/.corp/.home/.lan/.intranet/.private/.test/.example`）拒绝 → `socket.getaddrinfo` 解析后任一 IP 命中 `_is_blocked_ip`（私网/环回/链路本地/保留/未指定/组播）即拒绝；`_check_host` 异步版 DNS 经 `asyncio.to_thread` |
| 请求挂钩 | `event_hooks={"request": [_ssrf_on_request]}` 注入单例 client——**每个请求（含重定向跳）** 都过校验，拦截抛 `SSRFError` 中断请求 |
| 异常分类 | execute 捕获 `SSRFError` → `"请求被安全策略拦截（SSRF 防护）"`（业务失败，`error_code=None`） |

**取舍理由**：

1. **裸 IP 全拒（含公网）**：审查建议的保守策略，IP 目标无法验证归属，域名访问已覆盖正常网页浏览场景；
2. **hook 拦截优于手动跟随重定向**：httpx 请求级 hook 天然覆盖 `follow_redirects` 每一跳，避免手动 Location 循环的复杂度与遗漏；
3. **DNS 解析校验防 rebinding**：拒绝解析到内网站段的域名，本地私网域名无法绕过裸 IP 检查。

**语义边界**：

- 正常公网域名浏览不受影响（DNS 解析到公网 IP 放行）；
- DNS 解析失败即拒绝（fail-closed），避免「解析失败后直连」逃逸；
- 每次请求实时解析（不缓存），防 DNS rebinding；`to_thread` 避免阻塞事件循环。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/web_browse.py` | SSRF 防护块（`SSRFError` / `_is_blocked_ip` / `_check_host_sync` / `_check_host` / `_ssrf_on_request`）；`_get_http_client` 注入 `event_hooks`；execute 捕获 `SSRFError` | 新增 `tests/unit/test_web_browse_ssrf.py`（11 用例：裸 IP / 内网 TLD / 解析到内网 / 公网放行 / DNS 失败）+ `test_tool_execution.py` 新增 `test_web_browse_rejects_ssrf_target`（execute 级拦截 127.0.0.1，不真实请求） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) WebBrowseTool 安全限制行 + HTTP 客户端实现要点补 SSRF 说明 | — |

---

## 验证

- 全量测试 **498 passed**（63.36s，含 12 个新增 SSRF 用例），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **SSRF 防护三件套**：裸 IP 拒绝 + 域名解析后 IP 网段校验（防 rebinding）+ 重定向每跳校验（防逃逸）——缺一即留洞。
- **hook 级拦截覆盖全请求路径**：httpx `event_hooks["request"]` 在每次请求（含重定向）前触发，比仅校验入口 URL 更完备。
- **fail-closed 语义**：解析失败 / 无法判定一律拒绝，宁可误伤不放开内网面；正常公网浏览不受影响。
