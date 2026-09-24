# 数据库迁移核心设计说明

> 对应代码：`app/infrastructure/database_migrations.py`
> 文档定位：唯一迁移序列核心的内部协作契约与维护说明
> 更新日期：2026-09-23
> 状态与映射：[ALIGNMENT](../../ALIGNMENT.md) 的 `app/infrastructure/database_migrations.py` 条目；层次定位与模块地图见[基础设施说明](../infrastructure.md)
> 术语：迁移序列、事务、驱动与期限名词见[术语表](#术语表)

## 目录

- [数据库迁移核心设计说明](#数据库迁移核心设计说明)
  - [目录](#目录)
  - [设计目标与边界](#设计目标与边界)
  - [内部协作契约](#内部协作契约)
  - [核心概念与机制](#核心概念与机制)
    - [文件身份与序列完整性](#文件身份与序列完整性)
    - [历史前缀与精确 head](#历史前缀与精确-head)
    - [锁、原子性与整批执行](#锁原子性与整批执行)
    - [期限、取消与结算](#期限取消与结算)
  - [状态与执行流程](#状态与执行流程)
  - [关键实现说明](#关键实现说明)
  - [行为边界](#行为边界)
  - [离线命令生命周期](#离线命令生命周期)
  - [配置关联](#配置关联)
  - [验证入口](#验证入口)
  - [设计决策与问题记录](#设计决策与问题记录)
  - [术语表](#术语表)
    - [迁移文件与版本](#迁移文件与版本)
    - [SQL 与 PostgreSQL](#sql-与-postgresql)
    - [事务与驱动](#事务与驱动)
    - [期限与取消](#期限与取消)
    - [本项目用语](#本项目用语)
  - [相关文档](#相关文档)

---

## 设计目标与边界

本组件是唯一迁移序列的文件与历史事实来源，负责文件快照、完整历史比对和事务内单文件执行。DB-F03a 交付这些核心函数，DB-F03b 增加同文件的 `MigrationCommand` 管理离线资源与逐文件事务。它仍不提供完整 schema/readiness 检查；生产门禁缺失时 CLI 拒绝升级。

连接和逐文件事务的 Owner 在上层：

| 责任 | 归属 |
| --- | --- |
| 命令生命周期、逐文件事务的提交/回滚、提交结果未知、有界关闭与 CLI | DB-F03b 已实现；生产使用等待 F04 |
| 版本表自身结构、首迁移、旧表接管与受管结构/权限核验 | DB-F04 |
| 应用装配、readiness API 与运行入口 | DB-F05 |
| 真实 PostgreSQL 原子性与多语句回滚验收 | DB-F03 整体及 DB-F06 |

核心函数不创建版本表或池，不执行 commit/rollback/close；这些资源操作归独立的命令 Owner。两者均不做事务重试或解析 SQL。CLI 已提供入口，但只有上述门禁完备后才允许连接；runtime 应用接线归 F05。

---

## 内部协作契约

| 符号 | 输入 | 输出 / 异常 | 调用方责任 |
| --- | --- | --- | --- |
| `Migration` | 不可变 version / name / checksum / sql 文件快照 | 不可变值对象 | name 为完整文件名（含四位版本和 `.sql`）；sql 不进入 repr |
| `AppliedMigration` | 已登记的 version / name / checksum | 不可变值对象 | applied_at 属版本表结构，不参与文件身份比对 |
| `load_migrations(directory)` | 平铺迁移目录 | 已验证、按版本排序的非空 tuple；非法时抛 `MigrationError` | 目录为唯一平铺序列；只读取，不修改文件 |
| `validate_history(migrations, history, require_head=False)` | 本地快照与已登记历史 | 合法历史前缀长度；非法时抛 `MigrationError` | 历史按版本升序；升级允许前缀，版本就绪检查要求 `require_head=True` |
| `check_version_history(connection, migrations, deadline=...)` | 已持有连接和外层期限 | 只读检查全历史等于本地 head，返回 head | 还需组合结构/权限检查，不能直接作为完整 `schema_check` |
| `apply_next_migration(connection, migrations, deadline=...)` | 独占连接、已有真实事务且非 AUTOCOMMIT、可核验的驱动连接、外层期限 | 事务内执行并登记下一版本；已到 head 返回 `None` | 每次调用一个独立事务；返回版本号不等于已提交 |

文件名采用 `0001_name.sql`，名称为小写 ASCII 字母开头、字母数字及单个下划线分段。拒绝重复编号、从零起步、缺号和非法文件名。文件按原始字节计算 SHA-256，必须 UTF-8/LF、非空，拒绝 BOM、CR 和 NUL，不转换换行后计算校验和。非 SQL 文档忽略；不递归发现另一套工具迁移序列。发现阶段得到的快照在内存中不可变，执行阶段不重读已变化的文件；每个公开校验/执行入口仍完整核验传入快照。

---

## 核心概念与机制

### 文件身份与序列完整性

序列必须以版本 1 起步并连续，重复编号、缺号、从零起步或与文件名不一致的版本都在执行前被拒绝。本地序列与已登记历史都要求 `version` 为真正的整数（布尔值不算），避免 `True == 1` 之类比较掩盖非法数据。非法文件在任何数据库语句之前就被发现，因此“后续文件有问题”不会先执行前一文件。

### 历史前缀与精确 head

历史必须是从 1 开始连续、没有重复或未知版本、名称和校验和逐行匹配的前缀。校验包括所有已登记行与整个本地序列，不能只比较 `MAX(version)`。升级路径允许历史短于本地（`require_head=False`），版本就绪检查要求精确 head（`require_head=True`），只读检查同时拒绝过旧及超前历史。

### 锁、原子性与整批执行

`apply_next_migration` 在调用方事务内先对版本表加 `EXCLUSIVE` 锁（`NOWAIT`），再在锁内重新读取全部历史。锁用于拒绝迁移误并发，不是工具业务锁；每次锁后重读，其他执行器已推进的版本不会再次执行。拿不到锁即报 `migration_locked`，不排队等待。

整份 SQL 无参数交给 asyncpg，不按分号切割，因此字符串、注释与 dollar quoting 内的分号原样保留。仓库迁移 SQL 必须经审查保证可在事务内执行，不允许事务控制语句或非事务命令；本组件不实现 SQL 解析器。若批次意外结束事务则报 `transaction_lost`，但这不能撤销已发生的提交，也不能替代受信 SQL 的审查。

### 期限、取消与结算

deadline 必须是有限的 `asyncio` 单调时钟绝对时刻；传入 `NaN`、无穷或布尔值时报 `deadline_invalid` 且不发出任何语句。上层从[配置预算](#配置关联)计算整个命令及单文件可用工作期限，预留清理时间；核心不重置预算。核心使用 `timeout_at` 与驱动剩余 timeout，并在每个 `await` 后、以及两个成功出口返回前重新检查期限/取消，迟到执行不能继续登记（见 [DB-011](../../../issues/infrastructure/database/2026-09-23-migration-final-deadline-guard.md)）。

取消原样向上传播；超时转为稳定的 `timeout`。若底层永远不响应取消，`timeout_at` 不能独立保证控制返回有界：上层必须保有任务/连接 Owner，执行有限等待和必要终止，未完成不得丢弃资源。核心没有独立提交事实；任何错误后调用方必须回滚当前事务，不得捕获后继续提交。提交响应丢失属于上层“结果未确认”，下次显式运行先核对历史，不自动重放。

| 原因类别 | 稳定原因码 |
| --- | --- |
| 本地文件 | migration_files_unavailable / migration_files_missing / migration_filename_invalid / migration_sequence_invalid / migration_encoding_invalid / migration_checksum_invalid |
| 历史、权限、锁 | schema_missing / schema_mismatch / permission_denied / migration_locked |
| 事务、期限 | transaction_required / transaction_lost / deadline_invalid / timeout |
| 其他数据库异常 | migration_database_error |

`MigrationError` 文本不含路径、SQL、凭证或驱动异常链；未知程序错误不伪装为可恢复迁移故障。原始数据库错误的预期类型在核心脱敏，上层仍必须使用受控引擎日志配置，不能通过 echo 或通用异常日志绕过保护。

---

## 状态与执行流程

```text
上层：取得独占连接 → 开启单文件事务 → 完成结构/基线门禁
  ↓
apply_next_migration
  校验完整本地快照 → 拒绝无事务/AUTOCOMMIT
  → LOCK public.schema_versions IN EXCLUSIVE MODE NOWAIT
  → 锁内读取全部历史并验证 → 已到 head 则返回 None
  → 获取同一连接的 raw.driver_connection → 验证物理事务
  → driver.execute(完整 SQL 文本, timeout=剩余时间)
  → 再验期限/取消/物理事务 → 参数化 INSERT 版本行
  → 返回本事务内执行的版本号
  ↓
上层：提交成功后记录确认事实；失败则回滚并清理
```

SQLAlchemy 的 begin 可以只建立逻辑事务；先经方言执行锁语句，再检查 asyncpg 的 `is_in_transaction()`，防止绕过方言执行批次时落入隐式自动提交。raw proxy 只是借用，不关闭它，不另开 asyncpg transaction 或连接。

只读入口是同一套历史比对的非写入形态：`check_version_history` 不加锁、不写版本行，要求精确 head，结束时由调用方回滚或关闭连接。

---

## 关键实现说明

以下为维护者必须知道的非显然约束，实现见 `app/infrastructure/database_migrations.py` 对应符号。

- **逻辑事务不等于物理事务**：`connection.in_transaction()` 反映 SQLAlchemy 的记账状态，锁语句经方言真正发出后，再用 `driver.is_in_transaction()` 确认物理事实；批次执行后再验一次，用来发现“批次把事务结束了”。`AsyncConnection.sync_connection` 与池代理的 `driver_connection` 都是可选属性，读不到执行选项或拿不到驱动连接时按 fail closed 报 `transaction_required`，不落成属性访问错误——无法核验事务就不执行整批 SQL。
- **raw proxy 只借用**：`get_raw_connection()` 的代理不关闭、不另开 asyncpg 事务，整批 SQL 与版本行登记共用调用方那一个事务。
- **每个成功出口返回前复用 Guard**：`timeout_at` 的取消需要事件循环获得调度，覆盖不了“同步校验完成后立即返回”的窗口，因此 `check_version_history` 与 `apply_next_migration` 的到 head 分支在 `validate_history` 之后再次检查期限与取消（见 [DB-011](../../../issues/infrastructure/database/2026-09-23-migration-final-deadline-guard.md)）。不引入新计时器，也不延长预算。
- **公开入口重复校验传入快照**：调用方传入的 `Sequence` 在入口处转为 tuple 并完整重跑文件/序列校验，不假定快照已被 `load_migrations` 验证过。
- **锁后重读而非缓存**：历史只在锁内读取一次即用于本次执行，不在锁外预读后复用，避免第二个执行器的推进被忽略。
- **不实现 SQL 解析器**：迁移 SQL 的可事务性由仓库审查保证，核心只按整批文本执行；发现语句序列异常的代价高于在核心内维护一个不完整的解析器。

---

## 行为边界

| 场景 | 行为 | 约束或验证入口 |
| --- | --- | --- |
| 已到 head | 返回 `None`，不执行 SQL，不写版本行 | 只读检查与执行入口共用同一历史比对 |
| 无真实事务、AUTOCOMMIT 或读不到执行选项 | 报 `transaction_required`，不发出任何语句 | 逻辑事务、AUTOCOMMIT、无同步连接三种形态 |
| 仅逻辑事务或拿不到驱动连接 | 报 `transaction_required`；在锁与历史读取之后、整批 SQL 之前拒绝 | 仅逻辑事务、无驱动连接两种形态；`calls == ["lock","history","raw"]` |
| 两个执行器并发 | 后者拿不到 `NOWAIT` 锁，报 `migration_locked`，不排队等待 | 仅覆盖 55P03 到 `migration_locked` 的映射，无真实锁竞争断言 |
| 历史非法、超前或逐行不一致 | 报 `schema_mismatch`，不执行 SQL | 缺号/重复/未知版本/错名/错校验和/布尔版本/乱序 |
| 目录缺失或为空 | 报 `migration_files_unavailable` / `migration_files_missing` | fail closed，不当作“无迁移可做” |
| 序列缺号或文件名非法 | 报 `migration_sequence_invalid` / `migration_filename_invalid`，不进入数据库阶段 | 非法名与 `.sql` 目录有断言；符号链接按同一路径拒绝，仅由代码约束 |
| 期限已过、非法或在校验后耗尽 | 报 `deadline_invalid` / `timeout`，不启动下一条语句 | 含同步历史校验完成后不得越过期限返回成功 |
| 调用方取消 | 原样传播 `CancelledError`，不发出下一条语句 | 逐阶段（lock/history/raw/batch/register）取消 |
| 批次或外部取消被吞 | 迟到执行不得登记版本行 | 吞掉超时与吞掉外部取消两条路径 |
| 批次意外结束事务 | 报 `transaction_lost`，不登记版本行 | 不能撤销已发生的提交，依赖受信 SQL 审查 |
| SQL 或登记语句失败 | 异常上抛，本事务不得提交 | 回滚与清理归调用方 |
| 数据库错误 | 按 SQLSTATE 归类为稳定原因码，文本脱敏 | 缺表不因此建表；未知程序错误不伪装为可恢复故障 |

---

## 离线命令生命周期

`scripts.migrate` 解析参数后读取 Settings，`scripts.init_db` 只调用同一个 `main`。`--help` 不加载配置。正式命令、输出字段与命令层原因码清单统一见[部署文档](../../project/deployment.md#工具-schema-迁移)（`MIGRATION_REASONS` 是同一清单的代码载体，不在此重复枚举）。

`SCHEMA_PREPARER` 是 F04 的内部接线点，当前为 `None`；CLI 在创建引擎和连接之前返回 `schema_gate_unavailable`，没有跳过检查参数。测试注入仅证明命令控制流。受信 preparer 接收当前事务连接、完整快照、`baseline_existing` 和裁剪期限，验证/准备结构；通常返回 `None`，严格基线登记后仅返回版本 `1`。基线单独提交后下一事务才执行后续 SQL，preparer 本身不得提交、关闭或另开连接。门禁返回值不符合该契约时报 `internal_error`。

`MigrationCommand` 是一次性 Owner：持有一个引擎/连接，为每个文件建立独立事务，调用核心后提交，到 head 的只读核验事务回滚退出。失败只回滚当前事务，保留已经收到 commit 成功响应的版本；提交请求发出后才登记未确认版本（此前耗尽预算属未发出提交，报 `timeout`），响应丢失、取消或失败均不自动重试，也不声称回滚已证明远端未提交。复用同一命令对象是调用方缺陷，报 `internal_error`，不借用事务原因码。收尾依次回滚、关闭已启动连接、dispose；进入收尾时预算已过则不执行回滚、直接保守报告未完成，超过清理期限时尝试终止已登记驱动。

`MigrationTimeouts` 是不可变、关键字构造的预算集合，数值由 Settings 校验，模块不维护第二套默认值；`scripts.migrate` 由配置键直接构造它，键名漂移会在启动时失败。命令总期限覆盖 worker 启动、文件发现、连接、全部事务和退出；参数/配置解析属于前置检查。每个文件另裁剪事务期限并预留清理，失败清理不延长该文件期限。业务取消不能阻止必要回滚，重复取消仍传播；成功响应先保存，再检查期限，避免丢失已经发生的事实。

单靠 `asyncio.timeout_at` 不能终止吞取消的协程，`asyncio.run` 的退出也会等待残留任务。CLI 因而由父进程监督一个 spawn worker，worker 独占数据库资源，父进程仅接管最多 128 字节的白名单管道消息。总预算预留 terminate/join/kill 时间，迟到消息不能覆盖父进程已选定的失败。强退标记提交结果未知并保留已确认版本；没有强退且没有未确认提交时，父进程终态标签才改写原因码，避免用 `cancelled` 之类标签盖掉“提交结果未知”。若操作系统仍未确认子进程退出，输出 PID 后直接结束父进程，不宣称物理释放成功。这是进程调度条件下的有界等待机制，不是操作系统故障时的硬实时保证。

开发阶段的取消/收尾问题见 [DB-013](../../../issues/infrastructure/database/2026-09-23-migration-cancel-cleanup.md)，主终态覆盖问题见 [DB-014](../../../issues/infrastructure/database/2026-09-23-migration-terminal-preservation.md)，本轮验证发现的原因码语义与文档校正见 [DB-015](../../../issues/infrastructure/database/2026-09-24-migration-verification-followups.md)。

## 配置关联

迁移两项预算由命令 Owner 消费，本核心只接收已裁剪的 `deadline`，不读配置、不重置预算：

| 配置键 | 作用 | 与本核心的关系 |
| --- | --- | --- |
| `DATABASE_MIGRATION_TIMEOUT_SECONDS` | 单个迁移文件或基线事务总预算，含登记与清理 | 上层据此裁剪出单次调用的 `deadline` |
| `DATABASE_MIGRATION_TOTAL_TIMEOUT_SECONDS` | 一次迁移命令总预算，含连接、全部文件及最终释放 | 命令 Owner 扣减后向下传递 |

键、类型、默认值与约束只在[配置参考](../../config_doc/config.md#7-数据库配置)维护。`Settings.database_config` 同时包含迁移预算与连接凭证，不能整体展开为构造参数或写入日志；CLI 显式选择参数构造独立引擎，并复用 runtime 的实例日志脱敏。应用装配归 DB-F05。核心期限不证明不合作驱动已物理关闭，离线命令由上述进程监督接管退出。

---

## 验证入口

| 测试文件 | 覆盖的可观察行为 |
| --- | --- |
| `tests/unit/test_database_command.py` | 逐文件独立事务、基线独立提交、后续失败保留确认版本、提交未知不重试、取消/文件超时回滚、未启动连接、收尾挂死与主失败保留 |
| `tests/unit/test_database_cli.py` | 双入口、UTF-8、帮助不加载配置、路径独立于 cwd、配置/参数脱敏、缺失 schema gate 零引擎/零连接 |
| `tests/unit/test_database_cli_supervision.py` | 真实 spawn 的正常退出、挂死、崩溃、坏消息；确认/未确认版本保留及晚到结果不覆盖主失败 |
| `tests/unit/test_database_migrations.py` | 文件名/序列/编码在执行前拒绝、不可变字节快照与文件名保留、历史前缀与损坏历史拒绝、非法待执行文件阻断全部数据库工作、只读版本检查无写入且要求精确 head、锁后重读而非缓存、整批 SQL 与登记共用同一事务、无真实事务与 AUTOCOMMIT 与执行选项不可读与驱动连接缺失的拒绝、逐阶段取消与超时、吞掉超时或外部取消后不得登记、数据库错误脱敏、未知程序错误不可恢复、`transaction_lost` 不登记、批次或登记失败不提交、缺表不建表、同步历史校验后不得越过期限返回成功 |

测试用 fake 只证明控制流，不能证明 PostgreSQL DDL 与版本行实际提交/回滚；连续 fake 调用也不授权在同一真实事务运行多份迁移。当前没有首个 SQL 迁移文件或真实 PostgreSQL 成功证据，多语句中途失败与版本行的真实原子性仍是 DB-F03 整体及后续真实验收门槛，不能因核心单测或真实 spawn 测试通过而关闭。当前实现与接线状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准，运行结果见 [DB-F03a 评审](../../todo.md#db-f03a-review)与 [DB-F03b 评审](../../todo.md#db-f03b-review)。

---

## 设计决策与问题记录

| 记录 | 关联内容 |
| --- | --- |
| [DB-ADR-001 D2](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d2唯一迁移序列) | 唯一迁移序列、版本表与原子性验收门槛的决策正文 |
| [DB-ADR-001 D4/D5](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d4运行时与故障语义) | 运行时与公共 API 契约，界定本核心与运行时的协作边界 |
| [DB-011](../../../issues/infrastructure/database/2026-09-23-migration-final-deadline-guard.md) | 同步历史校验后遗漏最终期限 Guard 的闭环 |
| [DB-012](../../../issues/infrastructure/database/2026-09-23-migration-optional-attribute-guard.md) | 事务前置条件读不到可选属性（执行选项、驱动连接）时按稳定原因码 fail closed |

---

## 术语表

本表解释本文件用到的外部库概念与项目用语，供不熟悉 PostgreSQL 事务或 asyncio 的读者查用。正文已完整定义的机制（原因码、执行流程）只给指针，不在此重复展开。

### 迁移文件与版本

| 术语 | 说明 |
| --- | --- |
| 迁移（migration） | 版本化演进数据库结构的一份 SQL 文件。本项目把它当作从 1 开始、不可跳号的唯一序列，不做分支迁移。 |
| 唯一版本序列 | 全库只有一条平铺迁移序列（`migrations/` 目录），不设工具专属第二套；依据见 [DB-ADR-001 D2](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d2唯一迁移序列)。 |
| 版本号与文件名 | 版本号是文件名前四位（`0001`），两者必须一致；`name` 列固定保存完整文件名，因此改名等同于改变身份。 |
| 校验和（checksum） | 文件原始字节的 SHA-256。它让“迁移文件被改动过”在历史比对时暴露，而不依赖人工核对的版本说明。 |
| 历史前缀 | 已登记历史必须是本地序列从 1 开始、逐行名称与校验和一致的连续前缀；升级中允许短于本地，不允许错位。 |
| 精确 head | 本地序列的最后一个版本，且数据库已登记到它。readiness 要求精确 head；允许前缀只用于升级路径，见[历史前缀与精确 head](#历史前缀与精确-head)。 |
| 基线（baseline） | 给已存在的业务表直接登记起始版本，避免对存量库重放建表语句；首迁移与基线归 DB-F04。 |
| 幂等 | 重复执行与执行一次结果相同。已到 head 时再次调用返回 `None`，不写版本行。 |

### SQL 与 PostgreSQL

| 术语 | 说明 |
| --- | --- |
| `public.schema_versions` | 记录已应用迁移的唯一版本表。本核心只读它并追加版本行，不创建它（归 DB-F04）。 |
| 整批执行与分号 | 一份迁移的 SQL 作为单个字符串交给驱动，不按分号切割，因此注释、字符串与 dollar quoting 内的分号原样保留。 |
| dollar quoting | PostgreSQL 的 `$$...$$` 字符串字面量，内部可含分号与引号；按分号切分 SQL 的实现会在这里出错。 |
| `EXCLUSIVE` 锁与 `NOWAIT` | 表级排他锁与“拿不到立即失败”。本核心用它在执行前拒绝第二个并发执行器，拿不到即报 `migration_locked`。 |
| SQLSTATE | PostgreSQL 的五位标准错误码，前两位表示类别。到原因码的映射见[期限、取消与结算](#期限取消与结算)。 |
| 事务控制语句 | `BEGIN` / `COMMIT` / `ROLLBACK` 一类语句。它们会提前结束外层事务，因此不允许出现在受审迁移 SQL 中。 |
| DDL 的事务性 | PostgreSQL 的结构变更可随事务回滚，本项目据此让“执行 SQL + 登记版本”同属一个事务；这一点仍需真实数据库验证，fake 不能证明。 |

### 事务与驱动

| 术语 | 说明 |
| --- | --- |
| 逻辑事务与物理事务 | SQLAlchemy 的 `in_transaction()` 表示“已开始记账”，不等于驱动层已发出 `BEGIN`；asyncpg 的 `is_in_transaction()` 才是物理事实。两者都要验。 |
| AUTOCOMMIT | 每条语句单独提交的执行模式。它会让迁移脱离调用方事务，因此被显式拒绝。 |
| DBAPI 驱动与 asyncpg | 真正与数据库通信的库；DBAPI 是这套接口的标准，本项目唯一后端是 asyncpg。 |
| raw connection proxy | `get_raw_connection()` 返回的代理对象，用来取到底层 asyncpg 连接。本核心只借用它，不关闭它，也不另开 asyncpg 事务。 |
| `is_in_transaction()` | asyncpg 判断连接是否处于真实事务中。批次执行前后各检查一次，用来发现“批次把事务结束了”。 |

### 期限与取消

| 术语 | 说明 |
| --- | --- |
| 单调时钟与绝对期限（deadline） | 单调时钟只前进不回拨，适合度量时间跨度。`deadline` 是绝对时刻，各阶段减去当前时刻得到自己还剩多少预算，避免层层等待叠加上去。 |
| `asyncio.timeout_at(deadline)` | 到点取消当前任务的上下文管理器。取消依赖事件循环被调度，覆盖不了同步计算后立即返回的窗口，因此成功出口另做检查（见 [DB-011](../../../issues/infrastructure/database/2026-09-23-migration-final-deadline-guard.md)）。 |
| 取消（`CancelledError`） | 向任务投递的“请收尾退出”信号，不是普通故障。本核心不吞取消，原样向上传播。 |
| 吞取消 | 任务的异常处理或 `finally` 把取消信号吃掉后继续执行。此时迟到的语句不得再写版本行。 |
| 有界收尾 | 等待带明确上限，到点返回当前结论。核心自身不做收尾，连接与任务的收尾归调用方。 |

### 本项目用语

| 术语 | 说明 |
| --- | --- |
| readiness 与 fail closed | 检查不完整或无法确认时按“不可用”处理，不默认放行。本核心提供只读版本核验，但结构与权限检查缺失时不得据此宣称 schema 就绪。 |
| `DB-F03a` / `DB-F03b` / `DB-F04` | 迁移核心、命令 Owner、版本表与首迁移三个分片的编号，进度见 [DB-F 计划](../../todo.md#db-foundation)。 |
| `D2` | [DB-ADR-001](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md) 中“唯一迁移序列”决策的编号。 |
| ALIGNMENT | 实现、接线、文档与测试状态的唯一登记表，见[代码—文档—测试对齐](../../ALIGNMENT.md)。 |
| `G0-1` / `G0-2` | 通用不变量：单次自动执行有可证明的终止条件；已选定终态后不再启动新的业务副作用。本核心的成功出口必须在期限内，见 [DB-011](../../../issues/infrastructure/database/2026-09-23-migration-final-deadline-guard.md)。 |

---

## 相关文档

- [基础设施说明](../infrastructure.md) — 本层定位、模块地图与容器直管现状
- [数据库运行时设计说明](database.md) — 连接、池、准入与完整 `schema_check` 的协作契约
- [配置参考](../../config_doc/config.md#7-数据库配置) — 迁移两项预算与数据库连接参数的唯一主位置
- [DB-ADR-001](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md) — 迁移、运行时与生命周期接线的决策正文
- [DB-F 计划](../../todo.md#db-foundation) — 分片进度、评审与后续交接
- [代码—文档—测试对齐](../../ALIGNMENT.md) — 实现、接线与测试状态
- [DB-011](../../../issues/infrastructure/database/2026-09-23-migration-final-deadline-guard.md) — 同步历史校验后最终期限检查的问题闭环
