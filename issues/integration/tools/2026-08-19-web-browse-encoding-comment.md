# TOOLS-024 web_browse 编码注释称「检测编码」但实现描述缺失

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（注释一致性，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 13）
> **涉及模块**：`app/integration/tools/builtin/web_browse.py`（WebBrowseTool.execute）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

web_browse 响应解码编码策略（`response.charset_encoding or "utf-8"`）无注释说明，易被误读为「仅依赖 httpx 默认解码」——与「检测编码」的意图不符，注释缺失导致实现意图不明。

### 影响

维护者误读编码策略（以为无 charset 检测），编码 bug 排查困难。

### 根因

编码检测实现无注释，与「检测编码」语义未绑定。

---

## 修复方案

execute 编码区补注释：`Content-Type charset（charset_encoding）优先，缺失默认 utf-8；逐 chunk 按该编码解码（errors="replace" 防非法字节崩溃）`。

**取舍**：纯注释澄清，实现已正确（流式改造时已用 charset_encoding 检测）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/web_browse.py` | 编码区补策略注释 | web_browse 测试通过（无逻辑改动） |

---

## 验证

- 相关测试（web_browse）通过（无逻辑改动）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **非显然实现要注释策略**：编码检测 / 回退逻辑不注释，维护者会误读「默认行为」——注释绑定实现意图。
