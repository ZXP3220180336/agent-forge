# TOOLS-028 builtin 「惰性加载」注释与实现语义不符

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（注释一致性，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 18）
> **涉及模块**：`app/integration/tools/builtin/__init__.py`
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`__getattr__` docstring 写「惰性加载」，但模块底部 `__all__ = list(_discover_tools().keys())` 在包导入时**立即**执行全量发现——「惰性」表述与实现不符（发现非惰性，`__getattr__` 仅属性查找兜底）。

### 影响

维护者误读加载时机（以为按需发现），排障方向错误。

### 根因

注释沿用「惰性加载」措辞，未反映 `__all__` 立即触发的实现。

---

## 修复方案

注释改为准确语义：`__getattr__` docstring → 「属性访问兜底：普通属性查找失败后从自动发现结果返回」；[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) 三处「惰性访问」→「属性访问兜底」。

**取舍**：纯注释 / 文档修正，不改实现（`__all__` 立即发现是既有设计，注释对齐实现）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/__init__.py` | `__getattr__` docstring「惰性加载」→「属性访问兜底」 | 现有测试通过（无逻辑改动） |
| `docs/integration_doc/tools_doc/builtin_doc/builtin.md` | 「惰性属性访问」→「属性访问兜底」（3 处）+ 小节标题同步 | — |

---

## 验证

- 相关测试（发现 / 装配）通过（无逻辑改动）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **注释语义要对齐实现**：`__all__` 立即触发发现，就不能叫「惰性加载」——术语准确性避免误导（结合 comment-code-consistency 反馈）。
