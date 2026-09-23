# 同步历史校验后遗漏最终期限检查

> **ID**：DB-011 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P2
> **来源/范围**：DB-F03a 新实现的开发阶段复核；未发布，未接入 CLI 或应用。

## 现象与证据

初版在读取历史后检查 deadline，但同步计算完整历史校验和后，两个成功出口直接返回。若本地校验耗尽剩余预算，`check_version_history` 仍报告成功，`apply_next_migration` 的“已到 head”分支也报告正常完成。

先补确定性时钟测试：校验完成时将 loop.time 前进到期限之后，不实际睡眠。两项红测均显示 `DID NOT RAISE MigrationError`；原有 64 项通过。

## 根因与方案

`asyncio.timeout_at` 的取消依赖事件循环获得调度，不能覆盖同步计算后立即返回的窗口。参照 [Python asyncio timeout](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout)，保留外层超时上下文，同时在两个成功返回前复用显式 deadline/cancel Guard。无需新计时器或任务，也不延长预算。

## 实施与验证

两个入口在 validate_history 后重新执行 `_guard(deadline)`，再判定返回。`test_history_validation_cannot_return_success_past_deadline` 的两个参数场景转绿；迁移核心合计 66 项通过，相关数据库测试 194 项通过。全量结果归 [DB-F03a 评审](../../../docs/todo.md#db-f03a-review)。

此问题没有引发已知数据库写入；它影响期限内完成的判定。正式规则见 [G0-1/G0-2](../../../docs/engineering/ai-engineering-rules.md#g0)；当前协作契约见[迁移核心](../../../docs/infrastructure_doc/database_doc/migrations.md)。
