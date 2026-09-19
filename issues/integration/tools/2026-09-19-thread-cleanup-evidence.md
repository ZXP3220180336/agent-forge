# TOOLS-055：清理窗口内完成的线程证据丢失

日期：2026-09-19；状态：已修复；优先级：P1；来源：工作区审查探针。
范围：ToolAttemptHandle / Supervisor，线程取消后的结果接管。

## 现象与根因

`run_sync` 等待取消后，真实线程在清理窗口内完成。协程终态仍是 CancelledError，Executor 交付失败事实；Supervisor 因记录未移交且事实已确认而删除记录，唯一保存在线程结果列表中的成功值或异常随之失去 Owner。

## 方案与实施

取消 `run_sync` 等待时立即调用现有 `retain_record()`，保留尚未交给适配器解释的线程值和异常。Permit 仍在真实完成后释放；记录继续占用有限恢复条目。保持外层取消、超时语义，不把任意线程中间值当作工具成功，不新增恢复协议。

参照 [Python Future.cancel](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future.cancel) 的运行中工作不可取消语义，以及 [asyncio.shield](https://docs.python.org/3/library/asyncio-task.html#shielding-from-cancellation) 对调用方取消与底层任务的区分。采用已有独立 Owner 保存原始结果，而非取消等待即清空责任。

## 验证与教训

`test_thread_outcome_during_cleanup_keeps_recovery_owner` 用事件保证协程先结束、线程后完成，覆盖业务取消、硬取消、本地超时 × 线程成功/失败，共 6 例：修复前全部因记录丢失失败，修复后通过；同时验证 Permit 释放及恢复容量封顶。相关执行/接管测试 34 项通过。

教训：协程的失败事实交付不等于后台线程证据交付。清理窗口内和窗口外完成都应覆盖。
契约见 [execution](../../../docs/integration_doc/tools_doc/execution.md)，适用 G0-3/G0-4。
