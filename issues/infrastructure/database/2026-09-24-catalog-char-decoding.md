# PostgreSQL 内部 char 解码导致目录误判

> **ID**：DB-017 · **日期**：2026-09-24 · **状态**：已修复 · **优先级**：P2
> **来源/范围**：DB-F04 新增 catalog 检查的真实 PostgreSQL 18.6 验证，未发布。

fake 行使用字符串，而 asyncpg 将 PostgreSQL 内部 `char` 返回为 bytes。真实查询得到 `relkind=b'r'`、`relpersistence=b'p'`，初版与字符串比较会把合法表判为 schema_mismatch。首迁移、失败回滚、基线三个真实测试先失败。

在 SQL 查询边界将 relkind/relpersistence、attidentity/attgenerated、约束类型及 FK 动作显式转为 text，保留相同列别名；不增加 Python 双类型兼容分支。参照 [asyncpg 类型转换](https://magicstack.github.io/asyncpg/current/usage.html#type-conversion) 与 [PostgreSQL pg_class](https://www.postgresql.org/docs/current/catalog-pg-class.html)。原三项真实用例及后续迁移矩阵转绿，fake 仍只作为控制流证据。

教训是驱动真实类型边界不能由 fake 字符串证明。当前策略见[迁移说明](../../../docs/infrastructure_doc/database_doc/migrations.md#首迁移与严格基线)，汇总见 [F04 评审](../../../docs/history/completed-work.md#db-f04-review)。
