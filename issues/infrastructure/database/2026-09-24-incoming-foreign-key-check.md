# 严格基线遗漏额外入向外键

> **ID**：DB-019 · **日期**：2026-09-24 · **状态**：已修复 · **优先级**：P1
> **来源/范围**：DB-F04 开发阶段独立复审；catalog 基线及运行时结构判断。

初版只检查 conrelid 属于受管表的约束，允许有效内部触发器，因此其他表指向 sessions 的 FK 可以改变 DELETE/UPDATE 行为却通过校验。真实事务内创建外部测试表以 ON DELETE RESTRICT 引用 sessions，预期 SchemaError，实际 DID NOT RAISE；随后回滚整个测试事务。

增加按 confrelid 查询入向外键的校验，只允许唯一的 public.messages(session_id) → public.sessions(id)，其动作与有效性继续由 messages 出向校验负责。版本表和 messages 不接受入向 FK。参照 [PostgreSQL pg_constraint](https://www.postgresql.org/docs/current/catalog-pg-constraint.html) 的 conrelid/confrelid 区别；不删除约束或改动外部业务表。

新增六项策略单测及上述真实复现转绿，catalog 合计 50 单测加真实回归共 51 passed。当前契约见[迁移说明](../../../docs/infrastructure_doc/database_doc/migrations.md#首迁移与严格基线)，汇总见 [F04 评审](../../../docs/todo.md#db-f04-review)。
