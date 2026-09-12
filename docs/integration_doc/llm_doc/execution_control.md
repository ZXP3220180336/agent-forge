# execution_control 设计文档

> **模块**：`app/integration/llm/execution_control.py`
> **更新日期**：2026-09-10
> **职责**：llm 层内部共享的执行控制等待辅助（等待原语）
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。

---

## 定位

执行终止信号（`_StreamCancel` / `_DeadlineExceeded`，见 [error.md](error.md)）驱动的等待原语，供 retry 退避、整流 / 续接退避、reserve 排队、每次真实 create 的受控 await 共用（LLM-044）。定义于本模块（而非 errors.py / retry.py）避免组件间反向依赖：各 llm 内部组件依赖本模块，本模块只依赖 `errors.py` 的私有信号类型。

## 接口

| 函数 | 说明 |
| --- | --- |
| `_raise_if_aborted(cancel_event, deadline)` | 真实请求前快检（同步）：已取消 / 已到期 → 抛类型化终止信号（不发起请求） |
| `_abort_trigger(cancel_event, deadline)` | 等待 cancel_event 置位或 deadline（monotonic 绝对）到期，返回 `'cancel'` / `'deadline'`（cancel 优先） |
| `wait_with_execution_control(delay, *, cancel_event, deadline)` | 退避 / 整流等待：睡满 delay 返回；期间 cancel / deadline 先到 → 抛类型化终止信号 |
| `await_with_execution_control(factory, *, cancel_event, deadline)` | 受控 await 协程工厂（reserve / create 共用）：快检命中不执行工厂；终止先到 cancel task 并按 task 结局——重抛取消/清理期异常 → 抛类型化信号；吞取消以值迟回 → 返回该值（LLM-045，交调用方接管） |

## 行为边界

- `deadline` 使用 `time.monotonic()` 的绝对时刻，各层只透传，不重复计算持续时间。
- `cancel_event` 与 `deadline` 同时命中时，用户取消优先。
- 被控任务与终止信号同时完成时先返回任务结果，由调用方的后置检查决定退款或结算，避免丢失已经取得的 `Reservation`。
- 终止先到时取消并等待被控任务和内部 waiter，确保 reserve 的部分预留退款完成且没有后台任务泄漏。
- **迟回值接管（LLM-045）**：abort 判赢后被控任务若吞取消以值收尾（或 `cancel()` 因任务已收尾而 no-op、`await task` 取得迟回值）→ helper **返回该值**——资源所有权移交调用方，由调用方 abort 后置复查接管（reserve→cancel 退款 / create→关流或按 usage 结算）并完成业务收尾，与「同时完成」同一所有权路径。**迟回值不是业务成功**：返回任何值都不代表业务未终止，调用方须先接管、再收尾；`cancel_event` 单次调用内须为单向置位信号。
- 整体期限以 `_DeadlineExceeded` 表达，不使用会被传输错误分类为可重试的内置 `TimeoutError`。

## 测试

- [test_execution_control.py](../../../tests/unit/test_execution_control.py)：两原语直测——LLM-045 吞取消迟回值返回（cancel/deadline 双形态）、取消配合重抛、清理期异常、入口快检不执行工厂、wait 三分支。
- [test_llm_request_budget.py](../../../tests/unit/test_llm_request_budget.py)：reserve 排队取消与迟回退款、create 在途终止与保守结算、流式 / 非流式迟回值接管。
- [test_streaming_rectifier.py](../../../tests/unit/test_streaming_rectifier.py)：retry/续接退避 deadline、chunk 竞争与流关闭。

## 相关文档

- [LLM 模块接口](llm.md)
- [重试与熔断](retry.md)
- [流式整流](streaming_rectifier.md)
- [LLM-044 问题记录](../../../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)
- [LLM-045 问题记录（迟回值接管）](../../../issues/integration/llm/2026-09-09-execution-control-late-result-drop.md)

## 消费方

- `retry.py`：`RetryHandler.execute` 入口快检（`_raise_if_aborted`）+ 重试退避等待（`wait_with_execution_control`）
- `streaming_rectifier.py`：`_backoff_sleep` 退避等待 + `_drain` 四方竞争（`_abort_trigger`）
- `llm_service.py`：`_budget_guarded_call` 入口快检 + reserve / create 受控 await（`await_with_execution_control`）
