# 工具批次事实收集说明

> **对应代码**：`app/domain/reasoning/tool_batch.py`
> **文档定位**：Domain 内部事实所有权与批次隔离契约
> **更新日期**：2026-09-14
> **状态与映射**：[ALIGNMENT](../../ALIGNMENT.md)

## 定位与职责

`ToolBatchCollector` 是一次工具批次的可见事实 Owner。Integration 通过同步 `record()` 发布事实，ReAct 在结束或异常传播前通过 `snapshot()` 接管当前可得事实，并调用 `close()` 断开后续更新。它不执行工具、不持有共享许可，也不写数据库。

## 内部协作契约

| 方法 | 行为 | 调用方责任 |
| --- | --- | --- |
| `record(fact)` | 按 batch、当前 call、规范 operation、attempt 建键；只有更高 revision 覆盖旧快照 | Integration 必须先保留自己的副本，再同步通知 |
| `snapshot()` | 返回深复制的有序 tuple | 调用方可保存或修改自己的副本，不会污染 collector |
| `close()` | 幂等停止接收晚到更新，保留已经接管的快照 | 批次正常结束、类型化终止和生成器关闭都应调用 |

同一个规范 operation 被多个消费 call 引用时，`batch_id/tool_call_id` 仍保持各自协议身份，不能合并为一条工具消息。当前 Piece 只完成身份和事实接线；兄弟任务协调、有界清理和协议历史的最终提交分别由 C-02 后续 Piece ④、⑤完成。

## 验证入口

`tests/unit/test_tool_lifecycle_contract.py` 覆盖 revision 幂等、关闭边界和双向快照隔离；`tests/unit/test_tool_lifecycle_wiring.py` 覆盖 ReAct 批次身份、事实先接管及类型化异常传播。当前状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准。

## 设计依据

[TOOLS-ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md) S1、S3、S4、S8。
