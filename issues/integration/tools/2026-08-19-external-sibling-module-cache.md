# TOOLS-013 外部工具卸载仅清理自身模块，兄弟模块缓存残留致重载失效

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（正确性，多文件工具潜伏风险）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 次要项 2）
> **涉及模块**：`app/integration/tools/loader.py`（ExternalToolLoader 模块缓存管理）
> **关联文档**：[external.md](../../../docs/integration_doc/tools_doc/external.md)

---

## 问题描述

### 现象

`_unload_file` 只 `sys.modules.pop` 工具模块自身，其通过相对导入拉入的兄弟模块（如 `app.integration.tools.external._helper`）缓存残留。重载时工具模块重新执行但兄弟模块仍是旧代码，「变更 → 下次调用生效」对多文件工具静默失效。

### 影响

多文件外部工具（工具文件 + `_helper` 共享模块）热更新失效：改 helper 后工具行为不变，且难排查（无报错）。

### 根因

模块缓存管理只追踪工具模块自身，未追踪导入链上的兄弟模块；`sys.modules` 的陈旧条目在重载时命中。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| importlib / sys.modules 管理实践 | 插件卸载须清理其引入的全部模块（含依赖），否则陈旧缓存污染重载 |
| 依赖图 / 模块追踪 | 加载记录新增模块集，卸载按集合一并回收 |

**核心**：加载时快照 `sys.modules` 新增条目，卸载 / 回滚按快照集合清理。

---

## 修复方案（含决策取舍）

**决策**：`_load_file` 快照 + 卸载 / 回滚按集合清理：

| 改动 | 内容 |
| --- | --- |
| `_load_file` | exec 前 `before = set(sys.modules)`；exec 后 `imported = set(sys.modules) - before`（工具模块 + 兄弟模块）；成功路径记录 `_file_modules[path] = imported` |
| `_drop_modules(before)` | 清理快照后新增的 sys.modules 条目（失败 / 回滚 / 无工具路径复用） |
| `_unload_file` | `sys.modules.pop(自身)` → 遍历 `_file_modules[path]` 全部 pop（工具模块 + 兄弟模块） |

**取舍理由**：

1. **快照差分追踪**：不解析 import 语句，用 sys.modules 前后差分捕获全部新增模块（含间接依赖）——覆盖任何导入方式；
2. **卸载按集合清理**：`_file_modules[path]` 记录一次，卸载 / 回滚统一走集合，无遗漏；
3. **`import_module(_EXTERNAL_PKG)` 单独 try**：包本体不算兄弟模块（before 在其后快照），避免误清包。

**语义边界**：仅清理「本文件导入时新增」的模块（工具模块 + 兄弟），不触碰其他工具共享的预存在模块；真实 external 包（`_EXTERNAL_PKG`）不受影响。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/loader.py` | `__init__` 加 `_file_modules`；`_load_file` 快照 before / imported 追踪，失败 · 回滚 · 无工具路径用 `_drop_modules`，成功记录 `_file_modules[path]`；`_unload_file` 按集合清理全部模块 | `tests/unit/test_tool_loader.py` 新增 2 用例：`test_drop_modules_cleans_new_entries`（快照差分清理，原有模块保留）+ `test_unload_cleans_sibling_modules`（模拟兄弟模块，卸载后从 sys.modules 移除） |
| 文档 | [external.md](../../../docs/integration_doc/tools_doc/external.md) 单文件自包含约定（helper 变更随工具文件重载生效）+ 加载 / 卸载流程（快照 + 集合清理）+ 测试状态 20 → 22 | — |

---

## 验证

- 相关测试 **22 passed**（含 2 个新增兄弟模块清理用例）
- 全量测试待提交前确认（增量改动：模块缓存清理，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **卸载必须按导入集合回收**：插件 / 模块卸载只 pop 自身，依赖链上的兄弟模块残留会让重载命中陈旧缓存——加载时快照差分，卸载按集合清理。
- **不解析 import 语句**：sys.modules 前后差分天然覆盖直接 / 间接 / 任意导入方式，比解析 import 更健壮。
- **包本体与兄弟模块区分**：`_EXTERNAL_PKG` 在快照前已注册，不算「本次新增」——避免误清其他工具依赖的包。
