# TOOLS-038 security.md 文档状态与代码不符（脱敏表述 + 审计测试数）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档同步，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（A 类 #1 / #5）
> **涉及模块**：`docs/integration_doc/tools_doc/security.md`
> **关联文档**：[security.md](../../../docs/integration_doc/tools_doc/security.md)

---

## 问题描述

### 现象

1. 边界情况第 4 条写「密钥脱敏：**列为未来增强**（params 截断已降低泄露面）」——但 `security.py:61-74` 的 `_mask_sensitive`（词边界正则 + 嵌套递归）+ `record` 已在序列化前掩码敏感键，且本文档第 48 行核心概念已描述为已实现——**文档内部自相矛盾且与代码不符**。
2. 测试状态写 `tests/unit/test_tool_audit.py`（**8 用例**），实际文件 10 个测试函数——漏列 TOOLS-016 的敏感键掩码两例（`test_audit_masks_sensitive_keys` / `test_audit_redacts_sensitive_in_log`）。

### 影响

文档把已实现能力误标为「未来增强」，误导读者低估当前安全基线；测试用例数失真削弱文档可信度。

### 根因

TOOLS-016（审计脱敏）实现后，security.md 边界情况与测试状态节未同步。

---

## 修复方案

按「写当前状态」规范对齐代码：

- 边界情况第 4 条改为「已实现（见上方 ToolAuditor 审计敏感键掩码）」——不复述机制细节（一个事实一个家）
- 测试状态改 10 用例，补充敏感键掩码覆盖

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `docs/integration_doc/tools_doc/security.md` | 边界情况#4 改为已实现；测试数 8→10 补掩码用例 | `tests/unit/test_tool_audit.py`（10 用例） |

## 验证

- `tests/unit/test_tool_audit.py` 10 用例全过；全量 **542 passed**
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

## 教训沉淀

- **文档同步是修复的组成部分**：实现脱敏（TOOLS-016）后未同步边界情况节，导致「列为未来增强」残留——改代码必查同一文档全文的对立表述。
- **测试用例数写死后必须随测试变更更新**：新增测试后 grep 对应文档的「（N 用例）」计数。
