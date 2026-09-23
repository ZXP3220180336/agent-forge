# 未启动连接的清理造成永久不可用

> **ID**：DB-002 · **日期**：2026-09-22 · **状态**：已修复 · **优先级**：P2
> **发现来源**：DB-F02 新代码的独立生命周期审查；尚未接入应用或发布。
> **范围**：DatabaseRuntime 探针连接失败后的清理。

## 现象与复现

初版在 AsyncConnection.start() 失败后仍调用 close()。真实 SQLAlchemy 引擎连接绑定但不监听的本地端口，首次报告 connection_failed，第二次 probe 却永久变成 close_incomplete；对应回归测试在修复前失败。

## 根因及方案

包装对象存在不等于已取得连接；未 start 的 close 触发 AsyncContextNotStarted，被误当作资源清理失败。按 [SQLAlchemy AsyncConnection](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#sqlalchemy.ext.asyncio.AsyncConnection.start) 的启动边界记录成功事实，只有 start 成功才 close。方言初始化期间已经登记的驱动仍由 runtime 的终止路径负责，不直接丢弃。

## 实施与验证

补 `test_real_engine_connection_refused_is_not_ready` 的连续显式 probe 断言，再增加 started 标记和未启动清理分支。测试转绿，两次均报告连接失败或超时，不误锁为清理失败；不会自动重连。此用例不需要 PostgreSQL，也不证明真实 PostgreSQL 可用。

## 教训与关联

资源释放必须依据实际取得状态，不依据包装对象已构造。完整实现验证见 [DB-F02 评审](../../../docs/todo.md#db-f02-review)；正式约束见 [G0](../../../docs/engineering/ai-engineering-rules.md#g0) 和 [数据库 ADR](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md)。
