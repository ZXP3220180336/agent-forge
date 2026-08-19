# TOOLS-015 executor wait_for 超时对 to_thread 同步调用无法取消（无注释说明）

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（文档澄清，非 bug）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 次要项 4）
> **涉及模块**：`app/integration/tools/executor.py`（`_execute_with_retry`）
> **关联文档**：[executor.md](../../../docs/integration_doc/tools_doc/executor.md)

---

## 问题描述

### 现象

`_execute_with_retry` 的 `wait_for` 超时取消的是外层执行协程；工具内部经 `asyncio.to_thread` 包装的同步 SDK 调用（如 Tavily 搜索）**无法被取消**——线程池线程继续阻塞至底层返回，超时后资源不立即释放。代码处无注释说明，易被误读为「超时 = 调用已终止」。

### 影响

误读超时语义 → 以为资源已回收而重复发起请求 / 错误清理；实际为 `to_thread` + 超时的固有行为。

### 根因

缺文档化说明。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| `asyncio.to_thread` + `wait_for` 组合语义 | `to_thread` 包装的同步调用脱离事件循环取消机制；超时只中断等待，不中止线程 |

**核心**：代码注释明示固有行为，防误读。

---

## 修复方案

`_execute_with_retry` docstring 补超时语义说明（`to_thread` 同步调用不可取消、线程继续至返回、非泄漏）。

**取舍**：纯注释澄清，不改行为（不可取消是 `to_thread` 固有语义，无法在工具层规避）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/executor.py` | `_execute_with_retry` docstring 补 `to_thread` 不可取消说明 | 现有测试通过（无逻辑改动） |

---

## 验证

- 相关测试（`test_tool_executor_components.py`）**24 passed**（无逻辑改动）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **固有行为要注释明示**：`to_thread` + 超时的不可取消性是组合固有语义，不注释易被后续维护者误读为 bug 或资源泄漏。
