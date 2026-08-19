# TOOLS-025 web_browse HTML 实体未联动链接文本，`</a>` 后文本误计入链接

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（解析正确性，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 14/15）
> **涉及模块**：`app/integration/tools/builtin/web_browse.py`（`_HTMLToTextParser`）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

1. **实体不联动链接文本**：`handle_entityref` 直接 append 到 `_text_parts`，未更新 `_links[-1]` 显示文本（`<a href="/x">&amp;foo</a>` 链接文本丢失 `&` 部分），也未联动 `_last_was_block`——与 `handle_data` 行为不一致；
2. **`</a>` 后文本误计入链接**：`handle_endtag("a")` 不标记链接闭合，`</a>` 后紧跟的文本（`<a>link</a> 后续`）仍更新 `_links[-1]`，污染链接显示文本。

### 影响

链接列表文本不完整 / 混入链接外文本，Agent 拿到的链接标注失真。

### 根因

`handle_data` / `handle_entityref` / `handle_endtag("a")` 三处对「链接内文本」的处理未统一，缺 `_in_link` 状态。

---

## 修复方案

统一文本追加逻辑 + 链接闭合状态：

| 改动 | 内容 |
| --- | --- |
| `__init__` | 加 `_in_link` 嵌套计数（>0 表示在链接内） |
| `handle_starttag("a")` / `handle_endtag("a")` | `_in_link` ±1（endtag 后文本不再计入链接） |
| `_append_text(text)` | 新辅助：在链接内更新 `_links[-1]` 显示文本 + append + 联动 `_last_was_block` |
| `handle_data` / `handle_entityref` | 均走 `_append_text`（实体在链接内同样更新链接文本） |

**取舍**：`_in_link` 计数（非布尔）支持嵌套 `<a>`；`_append_text` 统一两类文本来源，消除行为分叉。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/web_browse.py` | parser：`_in_link` + `_append_text` 统一（handle_data / handle_entityref / handle_starttag a / handle_endtag a） | `tests/integration/test_tool_execution.py` 新增 2 用例：`test_web_browse_parser_entity_in_link`（实体更新链接文本）+ `test_web_browse_parser_text_after_anchor_not_in_link`（`</a>` 后文本不计入） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) 无需改（解析器细节未在文档展开） | — |

---

## 验证

- 相关测试 **5 passed**（web_browse + parser）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **同类处理统一收口**：文本追加（data / 实体）共用 `_append_text`，行为不因来源分叉。
- **标签闭合需状态**：`</a>` 后文本不计入链接，用 `_in_link` 计数（嵌套安全）而非「最后一个链接」启发式。
