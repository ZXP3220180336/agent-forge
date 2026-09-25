# 数据库结构校验说明

> 对应代码：`app/infrastructure/database_schema.py`
> 文档定位：迁移准备与只读检查共用的 catalog 校验契约与维护说明
> 更新日期：2026-09-25
> 状态与映射：[ALIGNMENT](../../ALIGNMENT.md) 的 `app/infrastructure/database_schema.py` 条目；调用方的编排与原因码聚合见[迁移核心设计说明](migrations.md)
> 术语：catalog、入向外键、auto 依赖等名词见[术语表](#术语表)

## 目录

- [数据库结构校验说明](#数据库结构校验说明)
  - [目录](#目录)
  - [定位与职责](#定位与职责)
  - [内部协作契约](#内部协作契约)
  - [受管结构契约](#受管结构契约)
    - [表](#表)
    - [约束](#约束)
    - [索引](#索引)
    - [序列](#序列)
    - [权限](#权限)
  - [关键实现说明](#关键实现说明)
  - [行为边界](#行为边界)
  - [验证入口](#验证入口)
  - [设计决策与问题记录](#设计决策与问题记录)
  - [术语表](#术语表)
  - [相关文档](#相关文档)

---

## 定位与职责

本组件是受管数据库结构的唯一只读判据：查询 PostgreSQL 系统目录（catalog），核对三张受管表、其约束与索引、`messages.id` 的序列，以及应用账号的使用权限。迁移准备 `prepare_schema` 与只读检查 `check_schema` 都经它做结构与权限判定，二者共享同一套比较规则，因此不存在"升级能通过、readiness 通不过"这类双标准。

| 责任 | 归属 |
| --- | --- |
| catalog 查询、结构比较、序列与权限判定 | 本组件 |
| 事务、锁、版本表创建、首迁移与基线登记、原因码聚合 | [迁移核心](migrations.md)的 `prepare_schema` / `check_schema` |
| 列类型、客户端默认值、`public` 限定的 ORM 侧契约 | [数据模型层说明](../model_doc/model.md) |
| readiness 如何接入本组件、账号权限矩阵的完整验收 | [数据库运行时说明](database.md)、[DB-F 计划](../../todo.md#db-foundation) F05/F06 |

本组件只借用调用方连接：不开启事务、不提交/回滚/关闭连接、不加锁、不创建或修改任何对象。发现差异一律拒绝，绝不自动修复——不 `ALTER`、不 `DROP`、不调用 `nextval`/`setval`。

---

## 内部协作契约

| 符号 | 输入 | 输出 / 异常 | 调用方责任 |
| --- | --- | --- | --- |
| `SchemaError(reason)` | 稳定原因码文本 | `RuntimeError` 子类 | 原因码属契约；调用方按码归类，不解析文本细节 |
| `managed_tables(connection, deadline)` | 借用连接、绝对期限 | 已发现的受管表名 `set[str]`；同名非普通表抛 `schema_mismatch` | 结果只表示"存在哪些表"，不表示结构合规 |
| `validate_version_table(connection, deadline)` | 同上 | 版本表合规时返回 `None` | 只在已持有版本表的事务内调用；版本表由调用方在此之前创建 |
| `validate_session_tables(connection, deadline, *, baseline=False)` | 同上；`baseline` 表示本次用于存量库接管 | 两表与序列合规时返回 `None` | `baseline=True` 要求调用方已排空旧 writer 与序列使用者并持有两表排他锁；表锁不能替调用方证明该前提 |
| `check_permissions(connection, deadline)` | 同上 | 权限齐备时返回 `None` | 只提供时点准入证据，不保证后续写入绝不失败 |

`deadline` 是 `asyncio` 单调时钟的绝对时刻，由调用方从[迁移预算](../../config_doc/config.md#7-数据库配置)裁剪后传入；本组件不读配置、不重置预算、不自行计时。

失败只经 `SchemaError` 上报，文本即原因码：

| 原因码 | 含义 |
| --- | --- |
| `schema_mismatch` | 表、列、约束、索引、序列或入向外键与契约不符 |
| `schema_missing` | 受管表不存在 |
| `permission_denied` | 权限矩阵不满足，或账号处于只读事务 |
| `deadline_invalid` | 传入期限是布尔值或非有限数（`NaN`、`±inf`），不发出任何语句 |
| `timeout` | 调用时或返回前期限已过，不发出后续语句 |

数据库错误不由本组件归因：驱动、连接与 SQLSTATE 类异常不被捕获，由调用边界按 SQLSTATE 映射为 `migration_database_error` 等码（见[迁移说明](migrations.md#期限取消与结算)）。取消（`CancelledError`）原样向上传播，本组件不吞取消。

---

## 受管结构契约

### 表

| 表 | 列（类型，可空性） |
| --- | --- |
| `public.schema_versions` | `version` integer NOT NULL、`name` text NOT NULL、`checksum` text NOT NULL、`applied_at` timestamptz NOT NULL |
| `public.sessions` | `id` varchar(36) NOT NULL、`user_id` varchar(64) NOT NULL、`title` varchar(200)、`system_prompt` text、`created_at` timestamptz、`updated_at` timestamptz、`status` varchar(20)、`meta` json |
| `public.messages` | `id` bigint NOT NULL、`session_id` varchar(36)、`role` varchar(20) NOT NULL、`content` text NOT NULL、`reasoning_content` text、`token_count` integer、`created_at` timestamptz、`meta` json |

表级要求：`public` 下的普通永久表（`relkind='r'`、`relpersistence='p'`），无行级安全策略、无继承与分区、无重写规则、无非内部触发器或已禁用的内部触发器。

以下差异一律拒绝，不做兼容：

- 列集合必须精确相等；多列、少列、类型或可空性不同、列带 identity/generated 属性，均拒绝。
- 除 `public.messages.id` 外，任何列都不允许有默认表达式；每列的排序规则必须等于其类型的默认排序规则。
- 唯一允许的默认值形态是 `messages.id` 绑定受管序列，即首迁移的 `BIGSERIAL`。ORM 侧的客户端默认值（Python callable）不是 server default，不进入 catalog 比较，其契约见[数据模型层说明](../model_doc/model.md)。

### 约束

| 表 | 允许的约束 |
| --- | --- |
| `schema_versions` | 主键 `(version)` |
| `sessions` | 主键 `(id)` |
| `messages` | 主键 `(id)`；外键 `(session_id) → public.sessions(id)`，`ON UPDATE`/`ON DELETE` 均为 `NO ACTION`、`MATCH SIMPLE`、已校验、非 deferrable |

约束数量与类型必须恰好等于上表：多一个 `CHECK`、`UNIQUE` 或第二个外键都拒绝，未校验（`convalidated` 为假）的约束同样拒绝。已校验的 `NOT NULL` 目录条目不算额外约束——PostgreSQL 17 起 `NOT NULL` 也记入 `pg_constraint`，比较时按此排除；未校验的 `NOT NULL` 则视为额外约束。

**入向外键单独核验**：其他表指向受管表的外键同样会改变删除/更新行为，只检查本表声明的约束会漏掉它。`sessions` 只允许唯一一条来自 `public.messages(session_id)` 的入向 FK，其动作与有效性由 `messages` 的出向校验负责；`schema_versions` 与 `messages` 不接受任何入向 FK。

### 索引

每张表的索引集合必须精确等于下表，且每条索引都必须是最朴素形态：btree、单列、非表达式、无部分条件、默认操作符类、默认排序规则、全升序，且 `indisvalid`/`indisready`/`indislive`/`indimmediate` 全为真。

| 表 | 允许的索引 |
| --- | --- |
| `schema_versions` | `(version)` 唯一主键索引 |
| `sessions` | `(id)` 唯一主键索引；`(user_id)` 非唯一普通索引 |
| `messages` | `(id)` 唯一主键索引；`(session_id)` 非唯一普通索引 |

### 序列

`public.messages.id` 必须由 `public.messages_id_seq` 提供，并在目录层面完全匹配：

- 类型 `int8`，`START 1`、`INCREMENT 1`、`MINVALUE 1`、`MAXVALUE 9223372036854775807`、`CACHE 1`、`NO CYCLE`，位于 `public`，名称 `messages_id_seq`；
- 列的默认表达式必须是与该序列自身对应的 `nextval(...)`，并经 auto 依赖绑定到 `messages.id`，而不是仅名字相同的另一个序列。

`baseline=True` 时额外只读核对序列取值：由 `last_value + is_called` 推导的下一个 ID 必须大于 `public.messages` 当前最大 ID，且不溢出 `int8`。该检查只读 `last_value`/`is_called`，不调用 `nextval`/`setval`，不消耗序列值。

非 baseline 路径只读序列目录，不读序列当前值，因此不要求账号持有序列 `SELECT` 权限。

### 权限

`check_permissions` 要求下列条件全部成立：

| 对象 | 权限 |
| --- | --- |
| schema `public` | `USAGE` |
| `schema_versions` | `SELECT` |
| `sessions`、`messages` | `SELECT`、`INSERT`、`UPDATE`、`DELETE` |
| `messages_id_seq` | `USAGE` 或 `UPDATE` |
| 当前事务 | `transaction_read_only` 为 `off` |

不要求迁移 DDL 权限，也不要求序列 `SELECT`：只读检查证明的是"应用账号此刻能读写业务表"，不是"能改结构"。区分探针自身设置的只读事务与账号默认只读/恢复模式的责任在调用方，见[运行时说明](database.md#探测的只读约束与预算)。

---

## 关键实现说明

以下为维护者必须知道的非显然约束，实现见 `app/infrastructure/database_schema.py` 对应符号。

- **驱动类型边界在 SQL 侧转换**：PostgreSQL 内部 `char` 经 asyncpg 返回 `bytes`（`relkind` 是 `b'r'` 而非 `'r'`），与字符串比较会把合法表判为不匹配。涉及字段（`relkind`、`relpersistence`、`attidentity`、`attgenerated`、`contype`、`confupdtype`、`confdeltype`、`confmatchtype`）在查询中显式 `::text`，不在 Python 侧加双类型兼容分支。fake 行使用字符串，只有真实驱动能暴露该差异（见 [DB-017](../../../issues/infrastructure/database/2026-09-24-catalog-char-decoding.md)）。
- **期限在同步窗口前后各查一次**：`_rows` 在发出语句前与取回结果后各检查期限与取消标记。`asyncio.timeout_at` 只能打断挂起的 `await`，覆盖不了"查询已返回、Python 侧比较尚未结束"的窗口，出口检查是必需的（同类问题见 [DB-011](../../../issues/infrastructure/database/2026-09-23-migration-final-deadline-guard.md)）。
- **逐表独立比较，无聚合报告**：`validate_session_tables` 顺序比较两张表，任一步失败立即抛出，不存在"收集全部差异再报告"的路径。调用方拿到的永远是第一个不符项。
- **入向外键是独立查询**：本表声明的约束（`conrelid`）与指向本表的约束（`confrelid`）分两次查询；只查前者会漏掉改变删除行为的外部 FK（见 [DB-019](../../../issues/infrastructure/database/2026-09-24-incoming-foreign-key-check.md)）。
- **不缓存、不重试、不修复**：每次调用完整重查，失败不重试、不修正、不写入任何对象。

---

## 行为边界

| 场景 | 行为 | 验证入口 |
| --- | --- | --- |
| 同名视图冒充受管表 | `schema_mismatch` | `test_view_cannot_impersonate_managed_table` |
| 受管表不存在 | `schema_missing` | 表级查询无行分支 |
| 额外列；类型、可空性、默认值、生成列、排序规则变化 | `schema_mismatch` | `test_version_columns_reject_write_semantic_changes` |
| RLS、继承/分区、触发器、重写规则 | `schema_mismatch` | `test_table_features_with_hidden_write_semantics_rejected` |
| 约束数量或语义不符（延迟性、目标列、FK 动作） | `schema_mismatch` | `test_foreign_key_semantics_must_match` |
| 额外入向外键 | `schema_mismatch` | `test_unexpected_incoming_foreign_keys_rejected`、集成用例 `test_incoming_foreign_key_cannot_change_managed_delete_semantics` |
| 索引非 btree、单列、表达式、部分、降序或非默认操作符类 | `schema_mismatch` | `test_index_semantics_rejected` |
| 序列参数、归属、绑定或名称不符 | `schema_mismatch` | `test_sequence_semantics_mismatch_rejected` |
| 基线库序列落后于存量最大 ID 或推导值溢出 | `schema_mismatch` | `test_baseline_checks_next_id_without_consuming_sequence` |
| 权限不足或账号处于只读事务 | `permission_denied` | `test_runtime_permissions_denied_is_explicit` |
| 期限已过 | `timeout`，不发出语句 | `test_expired_deadline_does_not_query` |
| 期限为布尔值或非有限数 | `deadline_invalid`，不发出语句 | 仅代码约束，无独立用例 |
| 调用方取消 | `CancelledError` 原样传播 | 调用方路径覆盖（`test_cancelled_baseline_never_registers`） |

---

## 验证入口

| 测试文件 | 覆盖的可观察行为 |
| --- | --- |
| `tests/unit/test_database_schema.py` | 视图冒充拒绝、过期期限不查询、序列语义与基线下一值、索引/约束/FK 语义、入向外键、权限拒绝 |
| `tests/unit/test_database_schema_preparation.py` | 调用方侧控制流：catalog 拒绝、取消或期限耗尽时不登记版本行，非事务连接不查 catalog |
| `tests/integration/test_database_models.py` | ORM 建表结果由本组件判定合规（真实 DDL 与 catalog 比对） |
| `tests/integration/test_database_migrations.py` | 真实结构不兼容、序列落后、入向外键改变删除语义时拒绝 |

fake 行只证明控制流，真实 catalog 证据来自集成测试。当前实现与验证状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准，运行结果与后续验收归 [DB-F 计划](../../todo.md#db-foundation)。

---

## 设计决策与问题记录

| 记录 | 关联内容 |
| --- | --- |
| [DB-ADR-001](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md) | 从迁移核心提取为独立模块的理由（迁移与只读检查是两个真实调用方，符合 E9）；版本表与结构验证的决策正文 |
| [DB-015](../../../issues/infrastructure/database/2026-09-24-migration-verification-followups.md) | catalog 验证条款与行为边界表述校正 |
| [DB-017](../../../issues/infrastructure/database/2026-09-24-catalog-char-decoding.md) | 内部 `char` 解码导致目录误判 |
| [DB-019](../../../issues/infrastructure/database/2026-09-24-incoming-foreign-key-check.md) | 遗漏改变写入语义的入向外键 |

---

## 术语表

| 术语 | 说明 |
| --- | --- |
| catalog（系统目录） | PostgreSQL 记录自身结构的系统表，如 `pg_class`、`pg_attribute`、`pg_constraint`、`pg_index`、`pg_sequence`。本组件据此判断结构，而不是靠 `IF NOT EXISTS` 或"同名表存在"。 |
| `relkind` / `relpersistence` | `pg_class` 中"关系种类"（`r` 普通表、`v` 视图、`S` 序列等）与"持久性"（`p` 永久、`u` 非日志、`t` 临时）字段。 |
| 入向外键 | 其他表指向本表的外键。它决定本表被删行时的行为，因此属于本表要核验的风险。 |
| 校验（validated）约束 | `convalidated` 为真表示约束已在存量数据上验证过；未校验的约束语义弱于声明，因此拒绝。 |
| auto 依赖 | `pg_depend.deptype='a'`，表示对象由列定义自动创建（如 `BIGSERIAL` 的序列）。用于确认序列确实属于该列，而非只是同名。 |
| 基线（baseline） | 为已存在的业务表直接登记版本 1，不重放首迁移；此时序列必须已经领先于存量数据。编排见[迁移说明](migrations.md#首迁移与严格基线)。 |
| fail closed | 检查不完整或无法确认时按"不合规"处理，不默认放行。本组件的所有未知差异都归入拒绝。 |

---

## 相关文档

- [迁移核心设计说明](migrations.md) — 调用本组件的 `prepare_schema` / `check_schema` 编排、锁与原因码
- [数据库运行时设计说明](database.md) — readiness 如何接入只读 schema 检查器
- [数据模型层说明](../model_doc/model.md) — ORM 侧列类型、默认值与 `public` 限定契约
- [基础设施层说明](../infrastructure.md) — 本层定位与模块地图
- [配置参考](../../config_doc/config.md#7-数据库配置) — 连接、迁移预算与数据库相关配置键
- [代码—文档—测试对齐](../../ALIGNMENT.md) — 实现、接线与测试状态的唯一登记表
