# TOOLS-014 loader _scan_lock 在生命周期钩子 await 期间持有，反向调用 execute 死锁

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（并发正确性，声明式约束）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 次要项 3）
> **涉及模块**：`app/integration/tools/loader.py`（ExternalToolLoader.scan_once）
> **关联文档**：[external.md](../../../docs/integration_doc/tools_doc/external.md)

---

## 问题描述

### 现象

`scan_once` 在 `_scan_lock`（`asyncio.Lock`，不可重入）内调用 `_load_file` → `await tool.on_load()`。若某工具 `on_load` 反向调用 `service.execute` → `maybe_refresh` → `scan_once`，同任务内二次加锁 → 死锁。当前示例 `http_api` 的 `on_load` 仅建 httpx client，未触发。

### 影响

潜伏并发死锁：外部工具生命周期钩子若反向操作工具执行（如钩子内自查其它工具），整个事件循环卡死。

### 根因

加载流程在不可重入锁内 await 用户代码（`on_load` / `on_unload`），用户代码无约束可反向进入加锁路径。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 不可重入锁 + 回调约束 | 持锁 await 用户回调时，声明回调内禁止反向进入加锁路径（编程约定兜底） |

**核心**：声明式约束——生命周期钩子内禁止反向调用 execute；`asyncio.Lock` 不可重入是硬限制，只能靠约定避免。

---

## 修复方案（含决策取舍）

**决策**：文档化约束（注释 + 编写约定），不改锁语义：

| 改动 | 内容 |
| --- | --- |
| `loader.py` | `scan_once` docstring 注明：锁内 await 用户 `on_load` / `on_unload`，钩子内禁止反向调用 execute（经 maybe_refresh → scan_once 二次加锁死锁） |
| `external.md` | 编写约定第 6 条：生命周期钩子禁反向调用 execute（TOOLS-014） |

**取舍理由**：

1. **声明优于改锁**：`asyncio.Lock` 不可重入是 asyncio 语义；改「扫描完成标志短路」增加状态复杂度，且钩子内反向调用本身是反模式——约定禁止最简洁；
2. **不影响正常流程**：约束只作用于用户钩子编写规范，加载 / 重载正常路径不变。

**语义边界**：此约束不强制（运行时无法拦截），靠文档 + 审查保障；若未来出现真实需求，再落「扫描标志短路」或「锁外调用钩子」。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/loader.py` | `scan_once` docstring 补锁内钩子反向调用死锁说明 | 现有 22 用例通过（无逻辑改动） |
| `docs/integration_doc/tools_doc/external.md` | 编写约定加第 6 条（钩子禁反向调用 execute） | — |

---

## 验证

- 相关测试 **22 passed**（loader，无逻辑改动）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **持锁 await 用户代码要声明约束**：不可重入锁内调用外部回调，必须文档化「回调内禁止反向进入加锁路径」，否则死锁潜伏。
- **声明式约束是低成本兜底**：不适用强制拦截时，注释 + 约定文档让约束可追溯、可审查。
