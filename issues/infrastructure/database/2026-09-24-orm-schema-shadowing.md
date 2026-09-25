# ORM 未限定 schema 导致写入未经验证的同名表

> **ID**：DB-018 · **日期**：2026-09-24 · **状态**：已修复 · **优先级**：P1
> **来源/范围**：DB-F04 独立只读复审；ORM 与迁移目标一致性。

迁移和 catalog 验证固定 public，但原 ORM 发出未限定 schema 的表名。真实测试在同一连接建立临时 sessions，并设置 search_path=pg_temp,public 后，经 ORM 插入；初版 public.sessions 行数为 0，预期 1，证明写入目标与检查目标不一致。

两个模型明确 `schema=public`，外键目标改为 `public.sessions.id`，不改变全连接 search_path。参照 [SQLAlchemy 显式 schema](https://docs.sqlalchemy.org/en/20/core/metadata.html#specifying-the-schema-name)；最小修复限定到已批准 public 契约，不扩展连接策略。六项默认值单测与真实 shadow 用例合计 7 passed，完整 ORM/catalog 集成矩阵继续覆盖。

不迁移或删除其他 schema 的同名数据。当前契约见[模型说明](../../../docs/infrastructure_doc/model_doc/model.md)，本轮证据见 [F04 评审](../../../docs/history/completed-work.md#db-f04-review)。
