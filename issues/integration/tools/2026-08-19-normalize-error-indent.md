# TOOLS-017 normalize_error 每行 strip 破坏 traceback 缩进

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（可读性，次要项）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 次要项 6）
> **涉及模块**：`app/integration/tools/result_processor.py`（ResultProcessor.normalize_error）
> **关联文档**：[result_processor.md](../../../docs/integration_doc/tools_doc/result_processor.md)

---

## 问题描述

### 现象

`normalize_error` 每行 `line.strip()` 去除行首缩进——traceback 的 `File` 行 / 代码块缩进全部丢失，与 docstring「保留换行结构（traceback 可读性），只压缩连续空行」不符。

### 影响

Agent 收到的错误信息 traceback 不可读（缩进丢失影响列对齐与代码上下文辨识）。

### 根因

`line.strip()` 同时去掉了行首（缩进）与行尾空白，未区分。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| traceback 可读性 | 保留行首缩进（代码块 / 栈帧对齐），只去行尾空白与空行 |

**核心**：去空行 / 行尾空白保留行首缩进——`line.rstrip()` 替代 `line.strip()`。

---

## 修复方案

`normalize_error` 过滤行改 `line.rstrip()`（去行尾空白，保留行首缩进）；空行判断仍用 `line.strip()`。

**语义**：首部前导空白经 `error.strip()` 去除（字符串首部无缩进意义）；中间行缩进完整保留。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/result_processor.py` | `line.strip()` → `line.rstrip()`（保留行首缩进） | `tests/unit/test_result_processor.py` 更新 `test_normalize_error_strips_blank_lines`（保留行首缩进）+ 新增 `test_normalize_error_preserves_traceback_indent` |
| 文档 | [result_processor.md](../../../docs/integration_doc/tools_doc/result_processor.md) normalize_error 描述 + 测试状态 10 → 11 | — |

---

## 验证

- 相关测试 **11 passed**（含 traceback 缩进保留用例）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **strip 的粒度要匹配语义**：去空白要区分行首（缩进有意义）与行尾（无意义）——`rstrip` 保留结构，`strip` 破坏结构。
- **docstring 与实现一致**：声称「保留换行结构」就不能用破坏缩进的 strip——实现随文档对齐。
