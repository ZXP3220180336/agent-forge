# close_all 迭代共享字典：并发修改抛 RuntimeError / 单个异常中断整批清理

> **状态**：✅ 已修复（2026-08-11）
> **优先级**：P1（中）
> **来源**：2026-08-11 审核修复 · 2026-08-16 从 client.md 提取归档
> **涉及模块**：`app/integration/llm/client.py`（`ClientManager.close_all`）
> **关联文档**：[client.md](../../../docs/integration_doc/llm_doc/client.md)

---

## 问题描述

### 现象

`for client in _instances.values(): await client.close()`——`await` 让出事件循环控制权时，并发 `register_config()`/`close_client()` 修改字典抛 `RuntimeError`；单个 `close()` 异常中断整批清理。

### 影响

close_all 崩溃（RuntimeError）或中途中断，连接池清理不完整。

### 根因

迭代共享字典 + await 让出控制权；单个异常未隔离。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 迭代隔离 | 遍历期间字典可能被并发修改——先 `list()` 快照再迭代，避免 RuntimeError |
| 异常隔离 | 批量清理时单个异常不应中断整批——逐项 try/except + 日志 |

---

## 修复方案（含决策取舍）

**决策**：先 `list()` 快照再逐个关闭；每个 `close()` 包 `try/except Exception` + `logger.warning` 隔离。

**修复要点**：

1. **快照遍历**：`for client in list(_instances.values())`——`await client.close()` 让出控制权时并发修改字典不抛 `RuntimeError`；
2. **异常隔离**：单个 `close()` 抛异常（连接池关闭失败）用日志隔离，不中断其余 client 与 `_pending_closes` 的关闭。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/client.py` | `close_all` 快照遍历 + 单异常日志隔离 | `test_client_manager.py` 并发修改/异常隔离用例 |

---

## 验证

- close_all 期间并发注册不崩溃；单个 close 失败不中断整批
- 全量测试通过（2026-08-11 修复时验证）

---

## 教训沉淀

- **await 让出控制权期间不要迭代共享字典**：先快照再遍历（`list()` 隔离迭代与修改）。
- **批量清理异常隔离**：单个 close 失败记日志继续，不中断整批。
