# TOOLS-016 审计日志完整序列化参数，敏感键（API Key / Token）落盘泄露

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（安全，次要项）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 次要项 5）
> **涉及模块**：`app/integration/tools/security.py`（ToolAuditor.record）
> **关联文档**：[security.md](../../../docs/integration_doc/tools_doc/security.md)

---

## 问题描述

### 现象

`ToolAuditor.record` 将完整 `parameters` 序列化（`json.dumps`，截断 1000 字符）写入结构化日志。若工具参数含 API Key / token / secret / password / authorization（如 http_api 的 `headers.Authorization`、search 的 api_key），明文凭据落盘。

### 影响

审计日志成为凭据泄露面：日志文件 / ELK 检索到明文 token，安全基线破坏。

### 根因

序列化前未对敏感键掩码。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 日志脱敏实践（OWASP 日志注入防护 / 安全审计） | 凭据字段（key / token / secret / password）序列化前掩码，保留键名与结构 |
| 词边界匹配 | `\b` 正则避免 `monkey` 等含 key 的普通词被误掩码 |

**核心**：序列化前递归掩码敏感键值（嵌套 dict / list 一并处理）。

---

## 修复方案

`ToolAuditor` 增加 `_SENSITIVE_KEY_RE`（`\b(api_?key|token|secret|password|authorization|credential)\b`，IGNORECASE）+ `_mask_sensitive`（递归，敏感键值 → `***`）；`record` 序列化前调用。

**取舍**：词边界正则（`monkey` 不误伤）优于子串包含；递归覆盖嵌套 headers / body 结构。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/security.py` | `import re`；`ToolAuditor._SENSITIVE_KEY_RE` + `_mask_sensitive`；`record` 序列化前掩码 | `tests/unit/test_tool_audit.py` 新增 2 用例：`test_audit_masks_sensitive_keys`（嵌套 / 词边界 / 非敏感保留）+ `test_audit_redacts_sensitive_in_log`（caplog 断言凭据不落盘） |
| 文档 | [security.md](../../../docs/integration_doc/tools_doc/security.md) 审计节补敏感键掩码说明 | — |

---

## 验证

- 相关测试 **34 passed**（含 2 个新增脱敏用例）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **审计留痕必须与脱敏共存**：全量留痕 + 明文凭据 = 泄露面；敏感键序列化前掩码是审计的强制项。
- **词边界匹配防误伤**：`key` 子串匹配会误掩 `monkey` 等普通词——`\b` 边界让掩码只命中真敏感键。
