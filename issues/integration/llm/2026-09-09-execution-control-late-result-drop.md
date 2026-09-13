# LLM-045 await_with_execution_control abort 判赢后丢弃迟回值（reserve/create 资源泄漏）

> 2026-09-13 后继：迟回流的 close/settle 所有权规则继续有效；业务取消收尾后的公开
> 结果统一为 `LLMCancelledError`，见 [LLM-050](2026-09-13-stream-business-cancellation-contract.md)。
> 状态：✅ 已修复 ｜ 优先级：P1（触发时 HTTP 连接 / RPM·TPM 预留永久泄漏） ｜ 发现：2026-09-09（用户最小运行探针确认） ｜ 模块：execution_control / llm_service / streaming_rectifier / reservation_limiter
> 关联：上承 [LLM-044](2026-09-08-execution-control-through-every-call.md)（execution_control 引入）· [ADR Decision 8](../../../adr/integration/llm/2026-09-06-request-context-budget.md)

## 发现

`await_with_execution_control`（reserve / create 共用的受控 await 原语）在 abort（cancel_event 置位 / deadline 到期）先到时执行 `task.cancel(); await task`，**吞掉 CancelledError 后无条件抛类型化信号**。若 factory 吞掉取消**正常返回一个值**（或 `task.cancel()` 因 task 已收尾而 no-op、`await task` 取得迟回值），该值被直接丢弃。影响：

- reserve 返回的 `Reservation` 无人 cancel/settle → RPM/TPM 预留永久泄漏（限流容量减少）；
- create 返回的 SDK `AsyncStream` 无人关闭 → HTTP 连接泄漏。

## 分析

1. **根因**：abort 分支假定 factory 是「取消配合」的（收到 CancelledError 后内部清理并重抛），把「`await task` 正常返回一个值」与「抛 CancelledError」都吞掉后统一抛信号——值被丢。helper docstring 已承诺「task 与终止**同时完成** → 返回 task 结果、调用方后置复查，不丢返回值」；abort 判赢后 task 仍以值收尾正是同一情形的微窗口（`asyncio.wait` 快照里 task 不在 done，随后 cancel no-op / 吞取消值返回），却是唯一违反该承诺的路径。
2. **asyncio 语义**：`Task.cancel()` 只是「请求取消」，在协程下一个 await 点注入 `CancelledError`；协程可捕获后继续并正常 `return`。「吞取消以值收尾」是合法来源，helper 无法靠「更用力取消」消除。
3. **契约统一**：abort 决定业务终态、迟回值决定资源所有权，**两者不互斥**。既有「同时完成→返回值」分支已让每个调用方具备 abort 后复查接管路径（reserve 第④步 cancel 退款 / generate 检查点 3 / 整流 `_drain` 关流 + settle）——迟回值应走**同一条**所有权路径，而非由 helper 另起机制。
4. **否决「cleaner 回调」方向**：「同时完成→返回值」已使 abort 与值并存不可避免；再加 cleaner 会让值-win 出现第二套机制（near-tie 返回值、strict-late 抛信号 + cleaner），reserve/create 的结算逻辑分裂到第三处。

## 工业级参照

- **CPython `Task.cancel()` 官方语义**：`cancel()` 请求取消，在任务下一次于 await 点暂停时注入 `CancelledError`；协程可捕获它、执行清理后继续，最终以正常值返回（该返回值经 `Task.result()` 可取）——这是「迟回值」的合法且可复现来源。见 <https://docs.python.org/3/library/asyncio-task.html#asyncio.Task.cancel>。
- **非目标**：factory **吞取消后永久不返回**（完全不合作的协程）不在本 issue 范围——helper 的 `await task` 仍会等待其真正结束，方向 A 不引入对不合作协程的额外超时。LLM-045 只解决「取消后**最终以值正常返回**」的资源丢失。

## 修复

`await_with_execution_control` abort 分支改**三结局分派**：

- `await task` **正常返回** → `return` 该迟回值：资源所有权移交调用方，由调用方 abort 后置复查接管并完成业务收尾（reserve→cancel 退款 / create→关流或 settle），与「同时完成」同一路径，不丢返回值；
- 抛 `CancelledError`（工厂配合取消、内部已清理后重抛）→ 抛类型化信号（`_StreamCancel` / `_DeadlineExceeded`，现状保持）；
- 抛其他 `Exception`（取消后清理期真实异常）→ 吞掉、抛类型化信号（abort 优先，现状保持）。

docstring 改写为调用方契约 + 前提：**返回任何值 ≠ 业务未终止**（迟回值不是业务成功）；`cancel_event` 单次调用内须单向置位；返回值到达即所有权移交，须在下一项外部副作用前复查 abort。其余 `BaseException`（SystemExit 等）不扩捕。调用方**执行逻辑零改动**：`llm_service._budget_guarded_call` 仅同步 ⑤ 状态机注释契约（create 配合取消 → settle(None)；create 吞取消迟回 → 返回交由 generate/整流接管，可能按 actual usage settle）。

## 验证

- helper 直测 `tests/unit/test_execution_control.py`（12 用例）：其中 2 例**修复前红**——factory 吞取消返回 sentinel（cancel/deadline 双形态）应**返回 sentinel 而非抛信号**；泄漏断言以确定性信号为主（factory finally / CancelledError 计数 / identity / 有限超时），`asyncio.all_tasks()` 差集仅辅助。
- reserve 跨层锁定（`test_llm_request_budget.py`，**修复前红**）：reserve 吞取消迟回 Reservation → `cancel_calls == 1`（第④步退款）、create 不被调用。
- 流式 create 迟回端到端验收（**修复前红**：`close_calls == 0`）：create 恰 1 次（整流/续接/fallback 均未发起）、`stream.close_calls == 1`、`settle_calls == 1`、`cancel_calls == 0`、按公开契约产出用户取消事件。
- 非流式迟回锁：create 吞取消迟回完整 response → `settle(actual usage)`（`last_actual == 8`）后检查点 3 仍抛 `LLMCancelledError`（迟回值不作业务成功返回）。
- 全量 `uv run pytest` 与 `uv run python -m scripts.verify_alignment` 通过。

## 教训

- **abort 决定业务终态、迟回值决定资源所有权，两者正交、不互斥**：迟回值是「请求实际已发生 / 资源已取得」的所有权凭证，**不是业务成功**。helper 不得丢弃；调用方须先接管（settle/cancel/close），再完成 abort 收尾。
- **helper 返回不代表业务未终止**：任何返回值都必须经调用方 abort 后置复查才可对外副作用；未来新增 `await_with_execution_control` 调用方必须自带该复查，否则引入新泄漏（方向 A 的显式前提，已写入 docstring）。
- **`Task.cancel()` 只是请求，不是保证**：协程可吞取消并正常返回，helper 不能假设工厂必然重抛 `CancelledError`。
