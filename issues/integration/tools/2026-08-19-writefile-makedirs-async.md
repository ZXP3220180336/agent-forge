# TOOLS-020 writeFile os.makedirs 同步阻塞事件循环

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（性能，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 9）
> **涉及模块**：`app/integration/tools/builtin/file_ops.py`（WriteFileTool.execute）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`WriteFileTool.execute` 的 `os.makedirs(dir_path, exist_ok=True)` 为同步阻塞调用，在 async 路径中短暂阻塞事件循环。

### 影响

写文件前目录创建阻塞事件循环（毫秒级，深层目录累积明显）。

### 根因

同步磁盘 IO 未移出事件循环。

---

## 修复方案

`os.makedirs` 改 `await asyncio.to_thread(os.makedirs, dir_path, exist_ok=True)`（同步 IO 放线程池）。

**取舍**：`asyncio.to_thread`（通用）而非 `aiofiles.os.makedirs`（依赖 aiofiles 版本支持），行为不变仅异步化。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/file_ops.py` | `import asyncio`；`os.makedirs` 包 `asyncio.to_thread` | 现有 file 测试 **9 passed**（roundtrip 父目录创建覆盖） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) writeFile 实现要点补异步化 | — |

---

## 验证

- 相关测试 **9 passed**（writeFile 行为不变）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **同步磁盘 IO 移出事件循环**：async 路径里的 `makedirs` / `stat` 等短 IO 也应收敛到 `to_thread`，保持事件循环不阻塞。
