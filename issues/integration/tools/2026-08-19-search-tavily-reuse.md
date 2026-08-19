# TOOLS-019 search 每次 execute 新建 TavilyClient，未复用

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（性能，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 7）
> **涉及模块**：`app/integration/tools/builtin/search.py`（SearchTool）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`execute` 每次 `TavilyClient(api_key=...)` 新建客户端，未复用——与 web_browse 的 httpx 全局单例（连接池复用）不一致。

### 影响

高频搜索下重复构造 SDK 客户端（含内部状态初始化）；与工具层「连接复用」惯例不符。

### 根因

TavilyClient 构造放在 execute 热路径，未做实例级缓存。

---

## 修复方案

`SearchTool` 加 `__init__`（`_client` / `_client_api_key`）+ `_get_client(api_key)`：实例级复用 TavilyClient，api_key 变化时重建。

**取舍**：实例级（非类级）缓存——避免类级共享导致测试 mock 污染 / api_key 混用；对齐 web_browse httpx 单例的复用意图。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/search.py` | `__init__` + `_get_client` 实例级复用；execute 改用 `_get_client` | `tests/integration/test_tool_execution.py` 新增 `test_search_reuses_tavily_client`（构造计数 = 1） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) search 实现要点补客户端复用说明 | — |

---

## 验证

- 相关测试 **5 passed**（含 client 复用用例，既有 mock 测试不受影响）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **热路径客户端要复用**：SDK 客户端构造放 execute 内，高频调用重复初始化——实例级单例 + api_key 变更重建，兼顾复用与正确性。
- **实例级缓存避免测试污染**：类级单例会让 mock 测试互相串，实例级随实例生命周期隔离。
