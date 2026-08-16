# 热切换旧 client 静默忽略 + 后台关闭 task 无引用/累积/失败静默（连接关闭追踪演进）

> **状态**：✅ 已修复（2026-08-09 / 08-11 / 08-16）
> **优先级**：P1（中，连接池泄漏 + task 泄漏）
> **来源**：2026-08-09/08-11/08-16 审核修复 · 2026-08-16 从 client.md 提取归档
> **涉及模块**：`app/integration/llm/client.py`（`ClientManager`）
> **关联文档**：[client.md](../../../docs/integration_doc/llm_doc/client.md)

---

## 问题描述

热切换 `register_config` 关闭旧 client 的追踪机制逐步演进，累积三个缺陷：

| # | 问题 | 后果 |
| --- | --- | --- |
| 1 | **热切换旧 client 静默忽略（2026-08-09）** | `register_config` 只 `_instances.pop(key)`，旧连接泄漏到 GC |
| 2 | **后台 close task 无引用（2026-08-11）** | `asyncio.ensure_future(old.close())` 返回值未保存——事件循环先关闭时 "Task was destroyed but it is pending" 警告，旧连接池关闭时机不可控；task 异常无人消费（"Task exception was never retrieved"） |
| 3 | **后台 close task 累积 + 失败静默（2026-08-16）** | `_closing_tasks` 无完成回调清理，多次热切换累积已完成 task 引用（内存泄漏）；`gather(return_exceptions=True)` 结果丢弃，后台关闭失败静默 |

### 影响

旧连接池泄漏（长驻进程多次热重配累积）；task 泄漏 + 异常无人消费；后台关闭失败无感知。

### 根因

关闭动作的时机/追踪不完整——无运行事件循环时静默忽略，有事件循环时后台 task 无引用/无清理/无异常消费。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 连接池复用 | `AsyncOpenAI` 内部持有 `httpx.AsyncClient` 维护 TCP keep-alive 连接池——关闭 client 即释放连接池；只清理引用不会发送 FIN，端口/socket 等 GC |
| task 生命周期 | 无引用的 task 在事件循环先关闭时产生 pending 警告；异常无人消费——后台 task 必须追踪 + 完成回调清理 + 异常消费 |

---

## 修复方案（含决策取舍）

**决策**：无运行事件循环 → 旧 client 进入 `_pending_closes`（`close_all` 统一关闭，可追踪）；有事件循环 → 后台 task 记录到 `_closing_tasks`（`close_all` 等待）；挂 `add_done_callback(_on_closing_task_done)` 完成回调。

**修复要点**：

1. **无 loop 分支**（2026-08-09）：`asyncio.get_running_loop()` 判无运行循环 → `_pending_closes.append(old)`，由 `close_all()` 统一关闭——不再静默忽略；
2. **有 loop 分支**（2026-08-11）：`asyncio.ensure_future(old.close())` 存入 `_closing_tasks`——`close_all()` 开头 `gather(*_closing_tasks, return_exceptions=True)` 等待 + 清空；
3. **完成回调清理**（2026-08-16）：每个后台 close task 挂 `add_done_callback(_on_closing_task_done)`——完成自动移除引用 + 消费异常记 `logger.warning`，不依赖 `close_all` 显式检查；
4. **close_all 等待顺序**：先等待后台 task（异常隔离），再快照遍历 `_instances`/`_pending_closes` 逐个关闭。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/client.py` | `register_config` 关闭追踪（_pending_closes/_closing_tasks + 完成回调）；`close_all` 等待顺序 | `test_client_manager.py` 热切换关闭/close_all 等待/完成回调清理用例 |

---

## 验证

- 热切换旧连接不泄漏；后台 task 无 pending 警告/异常无人消费；多次热切换不累积 task 引用
- 全量测试通过（2026-08-09/08-11/08-16 修复时验证）

---

## 教训沉淀

- **关闭动作必须可追踪**：无运行事件循环时旧 client 静默忽略 → 连接池泄漏——进入 `_pending_closes` 由 `close_all` 统一关闭。
- **后台 task 必须追踪 + 完成回调清理 + 异常消费**：无引用产生 pending 警告、异常无人消费、已完成 task 累积泄漏。
