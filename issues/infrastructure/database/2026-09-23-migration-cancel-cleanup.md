# 业务取消阻止迁移必要回滚

> **ID**：DB-013 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P2
> **来源/范围**：DB-F03b 开发阶段；未发布、未执行真实迁移。

## 现象与复现

在单文件 SQL 执行等待期间取消命令任务，初版复用业务 `_guard` 进入清理。测试 `test_cancel_during_execution_rolls_back_before_propagation` 先失败：期望 rollback 一次，实际零次；同期另外 10 项通过。任务仍带取消计数，Guard 直接拒绝必要回滚。

## 根因与修复

禁止开始新业务工作的取消 Guard 不适用于必要收尾。清理改用同一期限的时钟检查，在剩余清理预算内回滚、关闭连接、dispose，重复取消仍正常传播；失败才尝试终止驱动，且不宣称完整清理。参照 [Python 任务取消](https://docs.python.org/3/library/asyncio-task.html#task-cancellation) 的 try/finally 清理模式，不清除外部取消状态或引入无界 shield。

## 验证与边界

该测试修复后转绿，另覆盖文件超时回滚和提交中取消保留未知结果。fake 仅证明调用顺序，不证明远端回滚。全量证据见 [F03b 评审](../../../docs/todo.md#db-f03b-review)，资源契约见[迁移说明](../../../docs/infrastructure_doc/database_doc/migrations.md#离线命令生命周期)。
