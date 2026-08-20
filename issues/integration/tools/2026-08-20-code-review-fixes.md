# TOOLS-049 工具模块代码审查修复批次（5 重要 + 15 次要 + 文档同步）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P1（含 2 项安全缺口 + 1 项归因正确性）
> **来源**：2026-08-20 工具模块整体代码审查（四维度：正确性 / 安全 / 性能 / 规范可读性）
> **涉及模块**：`app/integration/tools/`（executor / security / base / loader / assembler / validator / result_processor / hooks）+ `rca/` + 外围（tool_gateway / templates/tools）+ `container.py`
> **关联文档**：[tools.md](../../../docs/integration_doc/tools_doc/tools.md) · [executor.md](../../../docs/integration_doc/tools_doc/executor.md) · [security.md](../../../docs/integration_doc/tools_doc/security.md) · [tool_service.md](../../../docs/integration_doc/tools_doc/tool_service.md) · [external.md](../../../docs/integration_doc/tools_doc/external.md) · [rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题总览

### 重要（5 项，全部修复）

| # | 问题 | 模块 | 类型 |
| --- | --- | --- | --- |
| 1 | 重试全败路径错误归因：业务失败后超时 / 异常，最终仍返回更早的业务错误 | executor | 正确性 |
| 2 | SSRF 边界盲区：CGNAT 共享地址段 100.64.0.0/10（RFC 6598）被 `_is_blocked_ip` 放行 | security | 安全 |
| 3 | 审计脱敏只覆盖 `parameters`，`error` 与 `content_preview` 明文落盘；`base._invalid_params_result` 回显完整 kwargs | security + base | 安全 |
| 4 | 外部工具冷启动对 LLM 不可见：`get_openai_tools()` 不触发扫描，首轮不调用工具则整个生命周期不可见 | tool_service/container | 正确性 |
| 5 | RCA 证据链时间锚点 `records[-1]` 依赖数据顺序，与「最新记录」注释语义不符 | rca ×4 | 正确性 |

### 次要（15 项，处理有实际价值的；取舍项保持现状或仅注释）

executor：execution_time `or 0.0` → `is None` 判断 / 信号量注释过时修正 / `max_retries` 语义注释。
loader：`_drop_modules` 只清 `_EXTERNAL_PKG` 前缀（第三方包单例漂移）。
assembler：单工具注册失败 try/except，docstring 契约落地。
security：敏感键正则覆盖驼峰 / passwd / 复数；DNS rebinding 注释降为「缓解」。
result_processor：`truncate` marker 计长注释；`normalize_error` docstring 改「删除空行」（行为被测试锚定）。
validator：`_field_name` 拼完整路径（嵌套字段保留父路径）。
tool_gateway：`ToolResult.__str__` 失败路径 `error or "未知错误"`。
templates/tools：提示词补「结果可能被截断」引导。
rca：FDC 判定阈值契约化落地（`_deviation_status` + `query_fdc` 派生 status）/ 窗口空结果归因区分（yield/fdc/alerts）/ history 删冗余 `int()`。
hooks：docstring 说明钩子应为 async。

**取舍项（保持现状）**：validator schema 缓存、嵌套 additionalProperties、getaddrinfo 超时、loader 重载竞态、scan_once 线程化、裸 IP 拒绝（ADR 保守策略）。

---

## 修复方案

### 重要项

1. **executor 重试归因**：`_execute_with_retry` 的 `TimeoutError` / `Exception` 分支置 `last_result = None`——全败收尾 `last_result or ToolResult(...)` 统一回退到最近一次失败（超时 / 异常优先于更早业务失败）。
2. **SSRF CGNAT**：`_is_blocked_ip` 返回条件追加 `not ip.is_global`（100.64.0.0/10 的 `is_*` 全 False 而 `is_global=False`，一次覆盖 CGNAT 及未来非公网段）。
3. **审计脱敏**：`ToolAuditor` 新增 `_mask_sensitive_text`（键值对保留分隔符 / `Bearer xxx` / `sk-xxx` 前缀），`record` 的 `error` / `content_preview` 走掩码；`base._invalid_params_result` 只回显键名。
4. **外部工具冷启动**：`container.initialize` 工具注册后 `await tool_service.refresh_external_tools()` 一次。
5. **RCA 时间锚点**：四处 `records[-1][...]` → `max(records, key=timestamp/sampled_at)[...]`。

### 次要项

见上表，逐文件实施（含注释 / docstring 与实现一致化）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `tools/executor.py` | 重试全败归因 + execution_time `is None` + 注释 | test_tool_executor_components（26） |
| `tools/security.py` | SSRF `not is_global` + `_mask_sensitive_text` + 敏感键正则 + DNS 注释 | test_web_browse_ssrf（24）/ test_tool_audit（13） |
| `tools/base.py` | `_invalid_params_result` 只回显键名 | test_rca_tools（校验不回显值） |
| `container.py` | 冷启动 `refresh_external_tools()` | test_tool_loader（扫描后注入 LLM 工具列表）/ test_container（装配后 11 = 10 内置 + 外部 http_api） |
| `tools/loader.py` | `_drop_modules` 只清 `_EXTERNAL_PKG` 前缀 | test_tool_loader（第三方包保留） |
| `tools/assembler.py` | 单工具注册失败 try/except | test_tools / test_rca_tools（装配不回归） |
| `tools/validator.py` | `_field_name` 完整路径 | test_tool_validator（13） |
| `tools/result_processor.py` | docstring 修正（truncate marker / normalize_error） | test_result_processor（11） |
| `tools/hooks.py` | docstring 钩子应 async | test_tool_hooks |
| `domain/ports/tool_gateway.py` | `__str__` error 兜底 | — |
| `domain/prompts/templates/tools.py` | 截断提示词 | — |
| `rca/{yield,fdc,defect,alerts}_tool.py` | 时间锚点 max + 窗口空结果归因区分 | test_rca_tools（22） |
| `rca/data.py` | `_deviation_status` + `query_fdc` 派生 status | test_rca_tools（FDC 阈值契约） |
| `rca/history_tool.py` | 删冗余 `int()` | test_rca_tools |

---

## 验证

- 工具相关测试 **171 passed**（executor / audit / ssrf / loader / result_processor / validator / rca / tools / hooks / selector / registry / http_api / approval）
- 全量 `uv run pytest` **556 passed**（含 test_container 装配断言更新：冷启动扫描后 10 内置 + 外部 http_api）
- `scripts/verify_alignment.py` 校验通过

---

## 教训沉淀

- **全败归因 = 最近失败**：重试循环里「业务失败 vs 超时/异常」是两种失败载体，收尾 `last_result or` 若不处理，会把最终超时归因到早前业务错误——归因类 bug 直接污染证据链 / 审计聚合。
- **安全枚举判定必有盲区**：`is_private/is_loopback/...` 枚举式网段判定无法覆盖全部非公网段（CGNAT 全 False），用 `not is_global` 作否定式兜底才完备。
- **脱敏要覆盖全部落盘路径**：只掩码 `params` 不够——`error` / `content`（工具抓取的自由文本）是凭据泄露的更大面，需文本级掩码 + 源头（错误信息回显）双管齐下。
- **「注释与实现一致」是真约束**：`_invalid_params_result` 的 kwargs repr 回显、`records[-1]` 锚点、`max_retries` 命名，都是「实现碰巧正确但语义脆弱」的典型——审查价值在消除这类脆弱性，而非只看表面正确。
