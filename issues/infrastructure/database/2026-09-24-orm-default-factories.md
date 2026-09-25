# ORM 默认值在导入时求值

> **ID**：DB-016 · **日期**：2026-09-24 · **状态**：已修复 · **优先级**：P2
> **来源/范围**：DB-F04 已批准的 ORM 一致性验收；Session/Message 客户端默认值。

`default=datetime.now(UTC)` 和 `onupdate=datetime.now(UTC)` 传入的是固定值，后续写入复用导入时刻；`default={}` 复用同一可变对象。SQLAlchemy PostgreSQL 编译/默认参数处理器的六项测试初始 5 failed、1 passed，修复后 6 passed。真实 PostgreSQL 用进程内恢复旧 ColumnDefault 的方式复现，两次实际 INSERT 的 created_at 相等，时间递增断言失败；工作区修复版通过。

按照 [SQLAlchemy Python 默认函数](https://docs.sqlalchemy.org/en/20/core/defaults.html#python-executed-functions)，改为时间 callable 和 dict 工厂，保持客户端语义，不增加 server_default、updated_at 插入默认或数据库触发器。时间、JSON 独立性及直接 SQL 默认边界由 `tests/integration/test_database_models.py` 验证。

这是已存在实现问题，不是实际生产数据损坏证据；已写入数据不自动修复。当前契约见[模型说明](../../../docs/infrastructure_doc/model_doc/model.md)，本轮结果见 [F04 评审](../../../docs/todo.md#db-f04-review)。
