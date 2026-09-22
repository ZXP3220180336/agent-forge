# 共享 PostgreSQL 基础设施

> **ID**：DB-ADR-001
> **日期**：2026-09-22
> **决策状态**：已批准。用户已审批本 ADR 及相关计划；D7 所列 TOOLS-ADR-008 条款替代生效，代码尚未实施。
> **实现状态**：未实施；当前源码与测试状态仍以 [ALIGNMENT](../../../docs/ALIGNMENT.md) 为准。
> **范围**：共享数据库运行时、统一迁移、已有 Session/Message 接入、可用性与资源关闭。
> **计划与授权**：[独立 DB-F01～DB-F06](../../../docs/todo.md#db-foundation)。设计与计划已批准；本轮按用户要求仅提交文档，实施按计划后续推进。

## Context

<a id="infrastructure-alignment"></a>

### 与既有基础设施说明的确认结论

本 ADR 以[基础设施说明](../../../docs/infrastructure_doc/infrastructure.md)为既有职责依据。初稿只读取该文档前部，遗漏后续规划的逐条对照；经用户指出后完整核对，并确认以下取舍：

- 保留 database.py 位置与 engine/sessionmaker 封装职责；业务用例依赖 Domain Store Port，数据库对象只供基础设施适配器使用。
- 沿用原规划的 init()/dispose() 命名，替代本 ADR 初稿的 start()/close()；保留轻量 ping，另以完整 probe 支撑 readiness。
- PostgreSQL 为本轮唯一后端，原说明中的 SQLite 降级候选不纳入此次建设。
- “置空后继续启动”保留为当前实现描述；目标为独立能力继续运行、持久化消费者不装配、相关 API 返回 503。首次启动数据库失败后的恢复需要重启应用；暂不实现热装配。
- 收集清理异常不能证明优雅退出；先排空使用方再释放数据库，未完成时保留依赖并明确报告。
- 共享数据库检查归独立 DB-F，Piece⑥消费底座并负责工具账本业务。

用户先确认上述对照取舍，随后批准相关文档并要求提交。设计批准与实现完成分别记录；本次提交仅包含文档，不执行数据库迁移或业务代码实现。

### 产品价值与核验事实

可靠保存对话和证据是 RCA 主链路及后续工具账本的共同依赖；本任务集中解决连接、事务、schema 和关闭责任，不实现工具业务恢复或长期记忆策略。

2026-09-22 在 HEAD `01d5267` 工作区核验：附件列出的 7 个文档修改均存在，未覆盖或恢复。`asyncpg` 未声明且当前环境无法导入；用不含真实凭证的示例 URL 构造引擎实测 `ModuleNotFoundError`。`database.py`、`scripts/init_db.py`、`scripts/migrate.py` 均为 0 字节。

本轮期间外部提交将 HEAD 推进至 `93c73c3`（仅收录上述 7 个文档的状态收敛），源码基线未变化；本执行者未运行 git commit，新增数据库设计由本轮文档提交收录。

Container 创建 engine 不连接数据库，失败后仍构造持有空工厂的 SessionManager。直接执行 `SessionManager(None, None).create_session(...)` 实测 `TypeError`，错误为 `NoneType is not callable`。`/api/health` 源码固定返回 ok；本轮未运行完整 ASGI 启动验证。

SessionManager 是 Application 中现存 SQLAlchemy 引用点，直接操作 ORM 和事务；仅移动 engine 不能满足本次分层约束。ChatService 没有全局排空接口，`_finalize` 清除取消登记后才保存消息，因此取消登记清空不是数据库使用结束的证明。

没有真实生产数据库或存量数据证据，不能据此推断不存在生产数据。本机 PATH 未找到 docker/psql/pg_isready，`127.0.0.1:5432` TCP 探测不可达；这不排除非默认端口或远端 PostgreSQL，但目前没有可用的真实验收环境证据。

### 工业级参照与取舍

| 官方参照 | 本项目采用的原则 |
| --- | --- |
| [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) | 共享 engine/sessionmaker；每个并发事务独立 AsyncSession；显式释放引擎，不能把构造当连通证据 |
| [SQLAlchemy defaults](https://docs.sqlalchemy.org/en/20/core/defaults.html) | 区分 Python default/onupdate 和数据库 server_default；首迁移忠实表达已有 SQL schema |
| [SQLAlchemy pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic) | pre_ping 检查取出连接时的存活，不能挽救事务中断或替代业务重试契约 |
| [asyncpg Connection API](https://magicstack.github.io/asyncpg/current/api/index.html#connection) | 驱动连接/命令/关闭设时限；无参数 SQL 批次可在执行器持有的事务中执行 |
| [PostgreSQL transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html) | 迁移 DDL 与对应版本登记同事务提交，失败回滚 |
| [PostgreSQL CREATE TABLE](https://www.postgresql.org/docs/current/sql-createtable.html) | IF NOT EXISTS 不证明旧对象结构匹配，不作为接管验证 |
| [PostgreSQL privileges](https://www.postgresql.org/docs/current/functions-info.html#FUNCTIONS-INFO-ACCESS-TABLE) | schema/table/sequence 权限分别核验，SELECT 1 不代表可写 |
| [PostgreSQL explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html) | 版本表事务锁只用于拒绝迁移误并发，不冒充工具执行 Owner |
| [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html) | 真实备选：成熟 revision/升级机制；本需求还需额外构建文件校验和、严格基线接管和运行核验 |
| [Python asyncio wait_for](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for) | 取消后的等待可能超出 timeout，单独套 wait_for 不能证明物理关闭有界 |

建议延续已有“版本化 SQL”方向，将执行器提升为共享基础设施。代价是本项目拥有发现、完整性校验和基线验证的测试责任。选择 Alembic 并非错误，但会改变已有决定，且仍需补充本需求约束；本轮不同时建设两套机制。

## Decision（已批准，待实施）

### D1：最小结构与领域边界

- 使用现有 `app/infrastructure/database.py` 承载 DatabaseRuntime：engine、pool、sessionmaker、探测状态、有界关闭。它有独立资源生命周期，满足 E9；不创建同名包。
- 新增 `app/infrastructure/database_migrations.py` 承载唯一迁移执行器及只读版本校验。迁移是独立的离线 schema 写入职责，两个 CLI 和 runtime 校验均有真实需求；不让 Container 内联 SQL。
- 新增 `app/domain/ports/session_store.py` 与 `app/infrastructure/session_store.py`，仅定义/实现已有会话、消息查询和写入所需方法。Session/Message 同属当前会话聚合，使用一个专用端口，避免破坏硬删除的单事务边界。无通用 Repository、DAO、UnitOfWork。
- SessionManager 保留业务编排与现有缓存行为，调用 Store，不持有 SQLAlchemy、asyncpg、ORM 或 DB session。Domain/Application 均不 import 上述数据库框架。框架无关输入输出沿用现有 dict/标识符，端口方法覆盖 create/get/list/list_v2、消息写入/最近历史、统计、软删除/硬删除。
- Application 选择操作的事务边界；适配器为每个操作创建 session 并提交/回滚。硬删除消息与会话必须在同一事务中完成，不能拆成两个独立提交。缓存操作不占用数据库事务；缓存策略变化不随本任务扩展。
- Store 转换持久化故障，Application 决定该用例不可用；API 只负责 HTTP 映射。数据库业务语义不进入 DatabaseRuntime。未来 Tool/Memory 自建领域端口和适配器，共享工厂但不共享会话实例。

### D2：唯一迁移序列

目标目录为 `migrations/0001_sessions_and_messages.sql`，未来全局下一版本由后续任务领取；现在不创建工具或记忆迁移。采用 UTF-8/LF 文件，SHA-256 按文件原始字节计算，不静默规范化后掩盖改动。

唯一版本表为 `public.schema_versions`，保存 `version`（主键整数）、`name`、`checksum`、`applied_at`（带时区）。文件名四位递增编号加名称；拒绝重复编号、缺号、非法命名、已登记名称/校验和变化、数据库未知版本和非连续登记。执行前先验证整个本地序列和全部已登记历史，再执行任何待运行迁移。

每个 SQL 文件及其版本行在同一事务中提交；中途失败仅回滚当前文件，先前已提交版本保留，下次显式运行从已确认前缀继续。提交响应丢失报“结果未确认”，不自动重放；再次运行必须先重读版本表。重复执行完整序列不重复建表或登记。

首个事务建立版本表并应用 0001；旧版本表存在时检查其自身结构。现有版本表使用事务内 `LOCK TABLE ... NOWAIT` 拒绝误并发，并在锁内重新核验历史。首建竞争允许一个执行器失败退出，不睡眠重试；版本间若被另一执行器推进，重新核验前缀后跳过已提交版本。此机制只保护迁移事务，不建设多实例工具锁/租约/Owner。

SQL 文件仅允许事务内迁移，不含 BEGIN/COMMIT/ROLLBACK、psql 命令、CREATE DATABASE、VACUUM 或 CREATE INDEX CONCURRENTLY 等破坏原子性的语句。仓库受信 SQL 由审查及真实回滚测试保证此限制，不编写脆弱的分号切割器或通用 SQL 解析器。通过 SQLAlchemy 所有的连接取得 asyncpg driver connection 执行无参数整批 SQL，外层事务、连接回收仍由执行器唯一负责；不得另开 asyncpg 连接池。DB-F03 必须真实证明多语句失败连同版本登记回滚。

`scripts/migrate.py` 是 CLI，`scripts/init_db.py` 委托同一入口，后者不调用 create_all。CLI 拟支持正常升级及显式 `--baseline-existing`；不保留 `--tools` 分支。实际使用说明只在部署文档维护。应用 startup 只执行只读检查，绝不自动迁移。

### D3：已有表基线与 ORM 对齐

| 数据库现状 | 处理 |
| --- | --- |
| 数据库尚不存在 | DBA/测试环境先创建数据库和账号；迁移器不负责 CREATE DATABASE |
| 数据库存在，受管表和版本表均不存在 | 正常迁移建立版本表与 0001；无关业务表不自动接管 |
| 仅有合法且空的版本表，无受管表 | 正常应用 0001 |
| Session/Message 均存在但无基线记录（有无数据均同） | 普通升级拒绝；显式 --baseline-existing 先锁定并完整验证，两表完全兼容才在事务中登记 0001，不改旧数据 |
| 只有一张表、结构不兼容、版本表不合法 | 拒绝并输出脱敏结构差异，不自动 ALTER、DROP 或补列 |
| 已有正式版本记录 | 按版本前缀、名称、校验和及所需结构核验，随后有序升级；baseline 参数不能绕过历史校验 |

基线验证范围：列集合、PostgreSQL 类型及长度、可空性、服务端默认值、PK、FK 目标及删除/更新动作、约束是否已验证、索引列/顺序/唯一性/有效性、messages 主键序列及归属与生成行为。拒绝会改变写入语义的额外约束、触发器或 RLS；不把同名视图当表。锁获取有界，发现不确定差异即拒绝，不建立泛化兼容框架。

显式接管要求旧写入方停止且旧连接不再使用该序列；表锁不能阻止独立 nextval 调用，无法满足此前提则拒绝接管。锁内只读核对序列 increment/min/max/cycle/cache 与首迁移预期一致，并由 last_value/is_called 推导下一值须大于现有最大消息 ID；落后或无法证明则拒绝。禁止用 nextval/setval 做检查或自动修复，它们的变动不会因事务回滚自动撤销，参见 [PostgreSQL sequence functions](https://www.postgresql.org/docs/current/functions-sequence.html)。

0001 保留现有 VARCHAR 长度、JSON（不改 JSONB）、TIMESTAMP WITH TIME ZONE、nullable、非级联外键，以及 `sessions.user_id`、`messages.session_id` 的索引。`messages.id` 使用与当前 PostgreSQL ORM DDL 一致的 BIGSERIAL/owned sequence。Python default 不是 server_default，不给 SQL 凭空增加默认值。

现 ORM 的 `datetime.now(UTC)` 在 import 时求值，`default={}` 也是需核验的可变默认值。DB-F04 先红测两个不同时刻的实际写入，再改为每次调用的时间和 dict 工厂；保持 created_at/onupdate 的客户端语义，不顺便给 updated_at 增加插入默认或 DB trigger。ORM 与迁移通过 PostgreSQL catalog 比对，并测试直接 SQL 默认行为；基线测试包含有数据兼容表，断言数据与序列行为保留。

### D4：运行时与故障语义

DatabaseRuntime 提供 `init()`、轻量 `ping()`、完整 `probe()`、受控 `session_factory` 和幂等 `dispose()`（目标接口，尚未实施）；工厂只供基础设施适配器使用。`ping()` 只检查真实连接和最小事务，不改变能力准入状态；`probe()` 复用这一检查并核对版本、结构及权限。生命周期状态为 new → ready/unavailable → closing → closed；关闭失败保留 close_incomplete 结果与资源责任，不伪报 closed。探针健康不是业务启用状态，两者分别记录。

init 顺序：构造 engine → 真实连接与最小事务 SELECT 1 → 版本前缀/名称/校验和 → 所需表结构 → schema/table/sequence 权限及只读事务设置 → 允许装配 Store。权限包含实际 CRUD 操作和自增序列需要的权限，不要求应用账号拥有 DDL。迁移账号通过同一个 DATABASE_URL 配置入口单独运行 CLI，不建设第二套配置系统。

运行时要求已应用版本恰好等于当前代码所带迁移序列的 head，且全部历史名称/校验和一致；缺失、落后或领先均为 not_ready。迁移器接受合法旧前缀以便升级，不代表 runtime 可以用该前缀开放能力。

startup 不写探针业务行、不修改 schema；权限目录和只读状态检查仅是当时的准入证据，真实写入仍可能因权限撤销、配额等失败，必须在 Store 边界明确转换。真实验收用测试数据证明事务提交与只读账号拒绝。运行中数据库错误不自动重试事务，提交结果不明不得报告为“确定未写入”。

连接、取池、探针/事务、迁移单文件、drain、回滚/连接关闭/dispose 均使用有限正时限。继续由 Settings 注入，完整键/默认值/约束在 DB-F01 的配置切片写入唯一配置参考；不在多个模块复制超时表。实现前冻结数值并测试零值拒绝、超时/取消与总预算不重置。总预算包含 cleanup，分步 timeout 不能叠加突破总 deadline；无后台无限重连。

资源唯一 Owner 为 runtime/当前事务适配器；取消向上传播，失败回滚与连接释放不得覆盖主异常。正常池释放超时走驱动支持的终止路径并报告失败；若仍有业务 Owner 不强行 dispose。控制返回有界与物理资源已释放分别验收，不用只返回 TimeoutError 冒充关闭完成。

只输出稳定原因码、异常类别和关联标识；不记录原始 URL、SQL 参数或底层异常全文/未过滤异常链。数据库日志禁用参数展示；DATABASE_ECHO 也不得绕过脱敏。认证、连接、schema、权限及关闭失败分别可诊断，但响应不返回主机、用户、密码或 SQL。

### D5：公共 API 与能力契约

| 接口/能力 | 建议契约 |
| --- | --- |
| GET /api/health | 保留 200 与现有 status/version，明确仅为 liveness，不调用数据库 |
| GET /api/ready | 新接口；每次在有限预算内真实探测，不只读取 startup 缓存；持久化依赖和消费者均就绪才 200，否则 503；Cache-Control: no-store |
| readiness 响应 | `status` 为 ready/not_ready；`database` 含 ready 与白名单 reason；`capabilities` 含 session_persistence、message_persistence、readonly_tools、side_effect_tools 四个布尔值；不输出连接信息 |
| 会话/消息以及依赖它们的 chat 请求 | 流前不可用时 HTTP 503，沿现有 `{code,message,details}` 信封，code 为 PERSISTENCE_UNAVAILABLE；无底层异常文本，无 NoneType 错误 |
| 流后保存失败 | 沿既有 SSE/应用错误出口表达失败，不尝试改已发送 HTTP 状态；不得报告持久化成功，也不重跑已结束 Agent/工具 |
| readonly_tools | 仅表示已装配、满足现有准入且不依赖 DB 的 A 工具，不保证外网工具此刻可达；数据库故障不统一关闭它们 |
| side_effect_tools | 本任务始终 false：账本/Owner/恢复尚未实现；基础库就绪不能开放 B |

reason 白名单拟为 starting、driver_missing、connection_failed、authentication_failed、schema_missing、schema_mismatch、permission_denied、timeout、restart_required、closing、close_incomplete；ready 时为 null。未知程序缺陷仍走内部错误，不伪装为可恢复数据库故障。

启动数据库不就绪时不创建 SessionManager/ContextManager/ChatService，API 依赖解析明确返回持久化不可用，LLM/ToolService 等独立能力可继续装配。首次启动失败后恢复需要重启装配；探针成功但消费者缺失时仍 503/restart_required。已成功装配后发生临时断连，后续显式探针可重新确认可用，业务事务本身不自动重放。

缓存不能替代 DB readiness 证明。已知数据库不可用时，持久化用例先拒绝，不靠旧缓存宣称完整持久化服务可用。就绪是时点观察，之后实际 Store 操作仍须处理断连与权限变化。部署将 /api/ready 用作完整持久化服务门槛可能切断同宿主 A 的流量，A-only 使用方需按能力选择路由，不能把这一差异隐藏在 liveness 中。

### D6：生命周期接线

启动按 D4 核验后注入 Store 再装配消费者；Container 不 import/create_async_engine，不承担 SQL 或迁移逻辑。

关闭顺序：关闭 Container/API 新业务准入 → ChatService 停止 prepare 并请求已有运行取消 → 等待运行与最终消息写入完成 → ToolService 有界关闭 → Store 使用方排空 → runtime dispose。过程中保留 LLM、Redis 和 DB 供已有 Owner 收尾。活动 chat 运行及 finalizer 要单独跟踪，不能以 TaskService 取消登记为空判定完成。

ChatService 最小新增准入状态与运行/finalizer 跟踪，并复用既有 ChatRun 关闭出口；不新建通用调度器。必须测试 prepare 与 shutdown 竞态、未消费流、正在消费流、重复 aclose、最终消息写入挂起/失败以及关闭取消。若 chat/tool 任一 Owner 未结束，返回关闭未完成并保留依赖；不继续 gather dispose。是否强退仍由后续受支持宿主负责，本任务不提前实现 Piece⑧，也不承诺整个进程必定物理退出。

<a id="database-supersession"></a>

### D7：具体替代关系（已批准生效）

本 ADR 仅替代 [TOOLS-ADR-008](../../integration/tools/2026-09-13-tool-execution-lifecycle.md) 以下条款：

1. S3 表中 scripts/migrate.py/init_db.py 的“SQL 位于 migrations/tools/、只改工具 schema、不接管整个数据库迁移”。改为本 ADR 的共享执行器与全局序列。
2. S7 的工具专属 `tool_schema_version`。改为唯一 `public.schema_versions`，工具业务表仍由 Piece⑥增加后续全局版本。
3. S7 将 asyncpg 驱动和迁移机制归入 B 新增的交付归属。前置能力改由 DB-F 交付，工具 CAS/事件幂等/恢复规则保持有效。
4. [部署规格](../../../docs/project/deployment.md#tool-lifecycle-p0) 对应的 `migrations/tools/0001_execution_ledger.sql` 和 `--tools` 命令，审批实施后统一更换入口。

上述旧条款已被部分替代，旧 ADR 保留历史正文并以指针标明后继决定；其余工具业务条款继续有效。架构蓝图中的 db/engine.py 只是候选路径，本任务按 E9 使用 database.py，实施时在原架构说明收敛这一局部选择，不重绘全系统。

## Consequences

收益：当前会话与未来账本/记忆可共享可验证的连接、版本和关闭责任；数据库错误在业务使用前明确暴露。代价：必须迁移 SessionManager 的 SQL 职责、补 chat 排空接线，并维护有限 SQL 执行器与严格基线测试。

不包含 ToolExecutionStorePort、工具账本/恢复/资源冲突保护、长期记忆表、向量库、SQLite、分布式执行锁或通用 Repository。没有真实旧库证据时不建立自动数据修复兼容层；将来出现分支迁移或多环境复杂升级需求，再单独评估统一切换 Alembic，禁止并行维护两种版本事实源。

## 实施与验证证据

本轮仅有上述只读源码核验、两项命令级失败复现与环境探测；尚无 pytest 红测、数据库连接成功、迁移或真实 PostgreSQL 验收证据。1513 passed 是附件给出的历史基线，本轮不据此宣称回归通过。

已批准的验证矩阵及文件分工见[独立计划](../../../docs/todo.md#db-foundation)。真实 PostgreSQL 门槛不能用 fake/SQLite 或跳过测试替代；环境缺失时 DB-F06 必须保持未完成。驱动依赖进入 DB-F01 的首个实现切片，避免 runtime 实现阶段仍不可加载。

## 关联记录

[基础设施说明](../../../docs/infrastructure_doc/infrastructure.md)是既有职责与当前说明入口，本 ADR 是数据库决策正文。

[产品导向](../../../docs/project/product.md) · [架构边界](../../../docs/project/architecture.md) · [工程 Gate](../../../docs/engineering/ai-engineering-rules.md) · [部署说明](../../../docs/project/deployment.md) · [配置参考](../../../docs/config_doc/config.md) · [数据库 ADR 索引](README.md)
