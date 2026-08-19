# TOOLS-026 web_browse 连续块标签换行观感（复核 + 测试锁定）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（可读性，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 16）
> **涉及模块**：`app/integration/tools/builtin/web_browse.py`（`_HTMLToTextParser`）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

审查观察：块级标签 starttag / endtag 各 append 一次 `\n`，可能产生连续空行（纯观感）。

### 复核结论

**实际已防重复**：`_last_was_block` 标志在 append `\n` 后置 True、文本后置 False——连续块标签（`<p>a</p><p>b</p>`）第二个 starttag 被跳过，不产生 `\n\n`；`get_text()` 的 `strip()` 进一步去除首尾空行。审查所述场景在现实现下不成立。

### 处理

补测试锁定行为（防后续回归），不改代码。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `tests/integration/test_tool_execution.py` | 新增 `test_web_browse_parser_no_extra_blank_lines`（`<p>a</p><p>b</p><ul><li>c</li><li>d</li></ul>` → `"a\nb\nc\nd"` 无连续空行） | **3 passed**（parser） |

---

## 验证

- 相关测试 **3 passed**（parser 含空行锁定用例）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **审查项复核先于修复**：`_last_was_block` 已防重复换行，审查观察在现实现下不成立——补测试锁定而非强行改代码（避免为不存在的 bug 引入风险）。
- **行为用测试锁定**：防回归的断言比代码改动更有价值，观感类问题以测试固化。
