# TOOLS-043 tool_service.md 内置工具枚举过时（5→10）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档同步，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（A 类 #11）
> **涉及模块**：`docs/integration_doc/tools_doc/tool_service.md`
> **关联文档**：[tool_service.md](../../../docs/integration_doc/tools_doc/tool_service.md)

---

## 问题描述

### 现象

使用示例注释「执行内置工具（search / readFile / writeFile / code_exec / web_browse）」仅列 5 个，实际内置工具已 **10 个**（RCA 5 个未列入）——枚举过时，易被误读为完整内置工具集。

### 影响

示例枚举误导读者（以为内置仅 5 工具）；RCA 工具已落地但示例未反映。

### 根因

RCA 5 工具（P0 落地）后，tool_service.md 示例注释未同步。

---

## 修复方案

注释改为「共 10 个：search / readFile / writeFile / code_exec / web_browse + RCA 5 个，此处以 search 为例」——反映现状且不随工具数漂移。

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `docs/integration_doc/tools_doc/tool_service.md` | 示例注释枚举 5→10 | 无（纯文档） |

## 验证

- 全量 **542 passed**
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

## 教训沉淀

- **工具数变化的锚点清单**：内置工具数从 5→10 是结构性变化，需 grep 全部「5 个工具 / 5 工具」表述（tool_service.md / selector.py / 测试断言）。
