# 项目待办

更新：2026-09-22。本文件只维护尚未关闭的工作记录；已完成工作的独特交接信息见[完成记录](history/completed-work.md)，具体缺陷与决策以当前 `issues/`、`adr/` 为准。执行流程只引用[项目工作流](engineering/project-workflow.md)，运行时判断只引用[运行时规范](engineering/agent-runtime-rules.md)。

<a id="db-foundation"></a>

## DB-F：共享数据库基础设施（DB-F01/02 已完成，后续待实施）

**DB-F02 参数整理**：按用户建议将六项运行时超时参数聚合为同文件的不可变 `DatabaseTimeouts`，保留字段名及秒单位，要求关键字构造且不定义第二套默认值/校验。① `database.py` 接收并持有该对象；② 两个运行时测试文件迁移构造方式，复用既有行为保护；③ 同步数据库组件说明、层入口与本记录。Settings、截止时间算法、资源生命周期和 DB-F03/05 范围不变；保留工作区已有文档整理。

参数整理已完成：`frozen=True, kw_only=True` 的配置值对象合并六个同属超时配置的构造参数，runtime 直接读取同一对象，未增加兼容分支或业务抽象。相关测试 115 passed；本次全量回归 1627 passed、5 xfailed、1 条既有警告（58.03 秒）；Ruff check/format、ALIGNMENT 与 diff 检查通过。组件正文已同步，未提交。

**DB-F02 当前授权与拆分**：用户已要求继续实施本片。先扩充 `tests/unit/test_database.py` 跑红测，再实现 `app/infrastructure/database.py` 的独立资源 Owner，最后同步基础设施说明、配置消费边界、ALIGNMENT、ADR 与本计划。使用 code-change / agent-lifecycle-review / documentation-maintenance；子智能体核对官方取消/释放语义、只读审查生产实现，并独立新增 `tests/unit/test_database_lifecycle_review.py` 三项真实 Session 回归。审查发现的开发阶段缺陷分别归 DB-002/003，索引与 lessons 同步。本片不改 Container、业务 Store、API 或迁移脚本，F01 中归属于后续装配/迁移的 strict xfail 保留。

F02 接口沿用 init/ping/probe/dispose。完整 schema 验证通过受控异步回调接入后续 F03/F04 的唯一检查实现；缺少该检查时不得开放工厂。版本读取只作观测，不以 MAX(version) 代替完整历史校验。探针与 dispose 各自最多持有一个资源工作任务，另有一个不执行 I/O 的清理阶段通知等待者；等待使用绝对期限和清理预留，取消不合作时保留 Owner、阻止新工作并报告 close_incomplete，不把 timeout 当物理资源释放。会话工厂重复调用复查准入，Session 从创建登记到 close 成功，池事件补充驱动/借出记录，外部 Owner 不强关，消费方 drain 归 F05。

**目标**：为已有 Session/Message、未来工具账本与长期记忆元数据提供同一 PostgreSQL 运行时和迁移底座。独立于 C-02 Piece⑥；完成 DB-F 不等于工具持久恢复完成。数据库决策唯一正文为 [DB-ADR-001](../adr/infrastructure/database/2026-09-22-shared-database-foundation.md)。

**授权历史**：用户先审批设计及对照取舍，文档已由 `2bb9c92` 提交；随后明确授权实施 DB-F01。原有 7 份文档修改由先前提交保留，本片基于干净工作区实施。

**DB-F01 当前授权**：用户已明确要求实施 DB-F01，范围为自动化失败复现、asyncpg 依赖/锁文件与配置；不提前实现 F02 运行时、F03 迁移或 F05 装配/API。先执行红测，再接入依赖及配置，最后跑相关/全量测试和工程检查。

本片细分：① 子智能体负责 Container/SessionManager/驱动/CLI 失败复现；② 主执行者负责 `tests/unit/test_database_settings.py` 与 `settings.py` 配置红绿测试、`pyproject.toml`/`uv.lock` 依赖；③ 同步配置参考、基础设施/部署现状、ALIGNMENT、ADR 和本计划。后续片未修复缺陷先用 `--runxfail` 留下红测，再以限定异常类型的 strict xfail 保留，不能计为本片已修复。配置值及完整约束以配置参考为唯一正文。

### 进度与实施顺序

- [x] 核验工作区、规范与当前代码；两项命令级失败复现；初查 PostgreSQL 环境。
- [x] 分别委托只读核查持久化链路和官方迁移方案；整合到 ADR，不建立第二份调研文档。
- [x] 形成独立分片、文件边界、readiness 契约与 TOOLS-ADR-008 具体拟替代条款。
- [x] 完整对照 infrastructure.md 并获用户确认：沿用 init/dispose、区分 ping/probe、采用 Store Port 边界、排除 SQLite、明确失败/关闭行为及独立 DB-F 归属；首次启动数据库失败后需重启恢复。已统一修订说明与 ADR，此次仅授权文档收敛。
- [x] 用户已批准 DB-ADR-001 和本计划，并要求提交相关文档；实现尚未开始。
- [x] DB-F01：先补自动化失败复现，再接入驱动/锁文件和配置；结果见[本片评审](#db-f01-review)。
- [x] DB-F02：DatabaseRuntime 与独立资源生命周期；结果见[本片评审](#db-f02-review)。
- [ ] DB-F03：唯一版本化 SQL 迁移执行器和 CLI。
- [ ] DB-F04：Session/Message 首迁移、基线接管及 ORM 一致性。
- [ ] DB-F05a：会话专用 Store Port / PostgreSQL 适配器，迁出应用层 SQL。
- [ ] DB-F05b：Container、readiness 与 503 错误边界；保持 A/B 能力分道。
- [ ] DB-F05c：聊天消费者停止准入、运行/finalizer 排空与数据库关闭顺序。
- [ ] DB-F06：真实 PostgreSQL 验收、全量回归与正式文档同步。

顺序为 F01 → F02 → F03/F04 → F05a/b/c → F06；F03/F04 配套形成可运行迁移切片，F02 在此之前只能验证资源契约，不宣称 schema 就绪。真实 PostgreSQL 环境优先准备，专项测试随 piece 运行，不留到最后才发现验收条件缺失。每个 piece 的源码修改前先给失败复现/行为保护，再实现；大于三个文件继续按下表小切片推进。

### 文件边界与验收

下列新增路径是待实施目标，用代码标记避免伪造现有链接；既有文件基于最新工作区增量修改。

| 切片 | 文件与职责 | 验收重点 |
| --- | --- | --- |
| F01a 失败复现 | `tests/unit/test_container.py`、`tests/unit/test_session_manager.py`；新增 `tests/unit/test_database.py` | 缺驱动、空工厂、连接失败仍声称就绪的当前失败证据；无迁移入口先通过 CLI/功能测试复现，不只断言文件大小 |
| F01b 依赖 | `pyproject.toml`、`uv.lock` | asyncpg 正式依赖、Python 3.14 兼容安装与真实导入；不顺便升级无关依赖 |
| F01c 配置 | `app/config/settings.py`、对应配置测试、`docs/config_doc/config.md` | 使用现有 DATABASE_* 入口，连接/池等待/操作/迁移/关闭时限有限且非法值拒绝；冻结初值并只维护一份配置表 |
| F02 运行时 | `app/infrastructure/database.py`、`tests/unit/test_database.py`、`tests/unit/test_database_lifecycle_review.py` | 创建、真实探测、工厂保护、状态、超时/取消/失败清理、重复关闭；engine 与工厂唯一 Owner |
| F03a 迁移核心 | 新 `app/infrastructure/database_migrations.py`、新 `tests/unit/test_database_migrations.py` | 文件顺序/名称/校验和、完整历史拒绝、只读版本校验；整批 SQL 不按分号切割 |
| F03b CLI | `scripts/migrate.py`、`scripts/init_db.py`、新 `tests/unit/test_database_cli.py` | 同一个入口、UTF-8 输出、有限退出、失败非零退出码、无 create_all/--tools 双机制；不泄露凭证 |
| F04a 首迁移 | 新 `migrations/0001_sessions_and_messages.sql`、`app/infrastructure/database_migrations.py`、新 `tests/integration/test_database_migrations.py` | 干净库、重复迁移、基线显式接管、结构不兼容拒绝、失败回滚；唯一版本表 |
| F04b ORM | `app/infrastructure/models/database/session.py`、`messages.py`、新 `tests/integration/test_database_models.py` | 先复现 import 时固定时间；改 callable defaults；字段/索引/FK/JSON/时区/自增及 server default 与 SQL 一致 |
| F05a-1 数据访问边界 | 新 `app/domain/ports/session_store.py`、新 `app/infrastructure/session_store.py`、新 `tests/integration/test_session_store.py` | 普通数据进出；实际 CRUD、消息最新窗口及 ID 排他上界、统计、增强列表、硬删两表原子性 |
| F05a-2 应用迁移 | `app/application/session/session_manager.py`、`tests/unit/test_session_manager.py` | 无 SQLAlchemy/asyncpg/ORM import；业务参数、排序、消息 ID 与缓存边界不漂移；无 DB 时不以缓存冒充持久化可用 |
| F05b-1 错误边界 | `app/shared/exceptions.py`、`app/api/middleware/error_handler.py`、`tests/unit/test_error_handler.py` | PERSISTENCE_UNAVAILABLE → 503 及既有信封；底层错误脱敏；程序错误与取消不吞成可重试故障 |
| F05b-2 装配 | `app/container.py`、`app/api/deps.py`、`tests/unit/test_container.py` | Container 不创建引擎；未就绪不构造伪可用 manager/context/chat；独立 A 工具保持可用，B 仍关闭 |
| F05b-3 API | `app/main.py`、新 `tests/unit/test_database_readiness.py`、`tests/integration/test_chat_flow.py` | health 只管存活；ready 实际探测与 200/503；启动失败恢复需重启；流前 503 与流后错误语义分别测试 |
| F05c-1 消费者生命周期 | `app/application/chat/chat_service.py`、`tests/unit/test_chat_service.py` | 新 prepare 准入关闭，活动 run/finalizer 有 Owner；同刻准备/关闭、未消费流、重复关闭、写入挂起/失败 |
| F05c-2 关闭接线 | `app/container.py`、`tests/unit/test_container.py`、新 `tests/integration/test_database_lifecycle.py` | chat/tool 未完成时不 dispose；最终落库先于连接关闭；所有等待有界；不把返回超时当实际释放 |
| F06 真实环境 | `tests/integration/test_database_runtime.py`（新增）及上述 PostgreSQL 测试；按需测试 fixture | 显式测试数据库、隔离受管表/账号、拒绝使用未声明的业务库；无 PostgreSQL 则记录阻塞，不能用 fake/SQLite 或 skip 算完成 |

测试 fixture 使用现有配置入口向测试注入专用数据库 URL；凭证通过环境安全提供，不写入计划/日志。环境准备不能对未经确认用途的数据库执行建表、改权限或清理。

### 文档同步切片

文档按分片事实同步，不把目标行为写成当前已实现。DB-F01 已同步依赖/配置与复现边界，其余随后续分片更新：

1. `docs/infrastructure_doc/database_doc/database.md`、`docs/infrastructure_doc/infrastructure.md`、`docs/infrastructure_doc/model_doc/model.md`：组件内部契约、层次定位与 ORM 契约；ADR 仍是数据库决策正文，组件文档不复制决策取舍，层文档不复制组件接口表。
2. `docs/application_doc/session_doc/session.md`、应用层导航，以及现有 chat 说明：Store 依赖和收尾责任；具体路径实施前按导航核对。
3. `docs/api_doc/README.md`、现有 API 契约文档、`docs/shared_doc/error_handling.md`：readiness 和基础设施不可用出口。
4. `docs/project/deployment.md`、`docs/config_doc/config.md`、`docs/project/architecture.md`：唯一使用命令、配置及本任务结构选择；以互链替代重复正文。
5. `docs/ALIGNMENT.md`：新增模块映射与实际状态；`DB-ADR-001`、数据库索引及 TOOLS-ADR-008：批准/兑现证据和具体替代生效。
6. `docs/todo.md`：逐项进度与评审；有红测闭环后按独立根因登记 Issue 及所属索引。仅发生真实纠正才更新 `docs/lessons.md`，不机械新增教训。

### 真实 PostgreSQL 与回归门槛

- 驱动导入、新库迁移、重复运行、名称/校验和变更、缺号/未知/过旧版本、迁移第二条语句失败时 DDL 与登记同时回滚。
- 已有兼容空表/有数据表严格接管、部分表/不兼容类型/缺索引/错误 FK/序列默认或归属差异拒绝；数据保留，消息新 ID 不与存量冲突。
- 首建竞争及已有版本表锁冲突有界失败；SQL 注释/字符串内分号；事务提交响应不明无自动重放。
- 真实创建/读取会话、保存消息、最近消息窗口/同时间 ID 排序/快照上界；软删、硬删原子性、统计与分页回归。
- 数据库不可达、认证失败、只读账号真实写入失败、序列权限不足、运行中断连/权限撤销；探针不是未来写入保证。
- engine dispose、取池超时、取消/回滚、关闭挂起、重复关闭、chat 最终落库顺序；日志、异常、CLI 输出不含凭证或完整 URL。
- health/ready/capability、503 信封、A-only 可调用、B 真实执行仍为零；Domain/Application 无 SQLAlchemy/asyncpg 引用。
- 按[部署与验证](project/deployment.md)依次运行相关测试、真实 PostgreSQL 专项、全量 pytest、Ruff check/format、ALIGNMENT 与 diff check；逐次记录真实输出，不引用历史通过数代替。

### 可选项与未授权范围

可选验收环境：已有专用远端 PostgreSQL，或本机 PostgreSQL/容器环境，依据用户可提供条件选择；没有可用环境时先明确阻塞。可选后续能力：启动失败后的热恢复、复杂迁移分支、额外 schema；本轮不实现。

禁止扩展为 Piece⑥账本、工具恢复/资源冲突、长期记忆业务表、向量库、通用 Repository/UnitOfWork、SQLite 降级或多实例执行锁；不同时引入 Alembic 与 SQL 执行器。

### 本轮设计评审

- 结论：单个 runtime 文件加独立迁移执行器足够；专用 SessionStore 是消除现存应用层数据库耦合的必要边界，chat 排空是满足关闭顺序的必要切片。命中 E1/E2/E3/E5/E6/E7/E8/E9；不新增事务自动重试，E4 无此新增触发。
- 证据：HEAD 与 7 个修改文件核对一致；asyncpg 缺失/引擎构造失败、空工厂调用失败已实际复现；三个入口占位为 0 字节。未运行全量测试或连接真实 PostgreSQL。
- 环境缺口：PATH 无 docker/psql/pg_isready，本机默认 PostgreSQL TCP 不可达，尚无远端测试库证据；DB-F06 保持未完成。
- 文档检查：`uv run --no-sync python -m scripts.verify_alignment` 通过（含相对链接检查）；`git diff --check` 通过。仅变更文档，未运行 pytest/Ruff，不宣称代码或真实 DB 验收通过。
- 设计复核：已补明确 runtime 要求版本等于当前迁移 head；基线必须停旧写入并只读验证序列下一值，禁止 nextval/setval 探针或自动修复。相关边界纳入真实 PostgreSQL 验收。
- 用户复核：初稿未完整对照 infrastructure.md 的规划部分，现已补全并记录[确认范围](../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#infrastructure-alignment)。后续已收到相关文档整体审批；已确认项不重复请求定夺，实施进度单独记录。运行时测试追加“ping 成功但版本/权限失败仍不可开放持久化”。
- 工作区变化：本轮期间外部提交 `93c73c3` 收录原有 7 份文档修改；已核对提交只改这些文档，源码未变化。该提交由外部完成；本轮按用户要求另行提交数据库设计与计划。
- 审批结果：统一 SQL 序列、严格基线接管、readiness API 与 chat 排空切片均随相关文档获批；后续直接按本计划推进，不将文档提交视为实现或真实 PostgreSQL 验收完成。

<a id="db-f01-review"></a>

### DB-F01 实施评审

**完成范围**：真实驱动导入/引擎构造的红绿测试；Container 未探测和空工厂、SessionManager 空依赖、两个空 CLI 的自动化失败复现；asyncpg 正式依赖及锁文件；数据库配置的有限时限、有限池容量、方言校验、环境注入和常规诊断凭证保护。未实现 Runtime、CLI 或 readiness API，不连接/迁移真实数据库，不实施 Piece⑥。

| 检查 | 实际结果 |
| --- | --- |
| 安装前驱动及缺陷复现，`pytest ... --runxfail -q --tb=short` | 7 failed：2 个真实驱动缺失；2 个 CLI 无帮助参数；2 个 Container 伪装配；1 个空工厂 TypeError |
| 配置红测，`pytest tests/unit/test_database_settings.py -q --tb=no` | 68 failed、2 passed；确认时限缺失、无界池未拒绝、URL/诊断缺少保护 |
| 配置修复，`pytest tests/unit/test_database_settings.py tests/unit/test_settings.py -q --tb=short` | 149 passed |
| 安装后相关复现集，`pytest tests/unit/test_database.py tests/unit/test_database_cli.py tests/unit/test_container.py tests/unit/test_session_manager.py -q --tb=short` | 33 passed、5 xfailed；2 个真实驱动用例转绿 |
| `uv add "asyncpg>=0.31.0"` 与实际 import | Python 3.14.6 / asyncpg 0.31.0；锁文件仅新增 asyncpg，无原有包升级或删除 |
| `uv run --no-sync pytest -q --tb=short` | 1584 passed、5 xfailed；1 条既有 Starlette/httpx 弃用警告 |
| `uv run ruff check .` / `uv run ruff format --check .` | 通过；232 个文件格式通过 |
| `uv lock --check` / `git diff --check` | 通过 |
| `uv run python -m scripts.verify_alignment` | 通过，含相对文档链接检查 |

上表 pytest 简写命令实际均经 `uv run --no-sync` 执行。

**未修复项去向**：5 个 strict xfail 仅匹配明确异常类型，意外通过会使测试失败；CLI 两项归 F03，Container 两项归 F02/F05，SessionManager 一项归 F05。原先把空工厂消费者视为正常降级的测试已改为目标契约，不保留错误成功断言。后续修复必须移除相应 xfail，不将其计为验收通过。

**Gate 与结构**：E6/E8 覆盖配置拒绝、诊断保护和先红后绿；配置直接使用现有 Settings/聚合属性，无新运行时类或兼容后端。URL 解析复用 SQLAlchemy，保留 str 配置类型与既有 Container 消费方式。没有新增数据库重试或事务行为；E3/E5 的实际运行时改造留待后续片。子智能体只读复核配置未发现阻断问题。

**边缘与限制**：覆盖池容量 0/-1/小数/布尔值、秒数 0/负数/NaN/无穷、环境字符串转换、合法转义凭证、非法驱动。配置导出及结构化 ValidationError 仍可能含原始输入，正式文档明确安全诊断方式；当前 Container 原始异常日志与新增预算执行尚待 F02/F05。真实 PostgreSQL 验收未运行，DB-F06 保持未完成。驱动根因及闭环见 [DB-001](../issues/infrastructure/database/2026-09-22-missing-asyncpg-dependency.md)。

<a id="db-f02-review"></a>

### DB-F02 实施评审

**完成范围**：独立 DatabaseRuntime 的唯一 engine/sessionmaker、init/ping/probe/dispose、只读连接探测、版本观察、受控工厂、Session/驱动 Owner、有限等待、取消与迟到处理、物理关闭核验及日志脱敏。没有修改 Container、Store、API 或迁移入口；未提供完整 schema 检查器时工厂保持关闭。

| 检查 | 实际结果 |
| --- | --- |
| 实现前 `pytest tests/unit/test_database.py -q --tb=short` | 1 failed、2 passed；缺少 DatabaseRuntime 的自动化红测 |
| 独立审查后的回归红测 | 连续连接拒绝、并发 dispose 失败、外部驱动初始化中关闭三项失败；新增 Session 建连前关闭保护单项失败，修复后转绿，详见 DB-002/003 |
| 末次边界复核红测 | ping 超时且工作任务仍在清理时，缓存工厂仍能创建 Session；修复为阻止工厂调用和新 checkout。另复现清理超时覆盖认证失败，改为保留先发生的稳定原因码；均已转绿 |
| `pytest tests/unit/test_database.py tests/unit/test_database_lifecycle_review.py tests/unit/test_database_settings.py -q --tb=short` | 115 passed；其中运行时与 Session 生命周期 45 项、配置 70 项 |
| `uv run --no-sync pytest -q --tb=short`（最终代码） | 1627 passed、5 xfailed、1 条既有 Starlette/httpx 弃用警告；54.45 秒 |
| `uv run --no-sync ruff check .` / `ruff format --check .` | 通过；233 个文件格式通过 |
| `uv run --no-sync python -m scripts.verify_alignment` / `git diff --check` | 通过，含最终记录与相对链接 |

pytest 简写均经 `uv run --no-sync` 执行。全量回归按最后一次实现修复重跑，上表记录最终结果。

**Gate / 结构**：E3/E5/E6/E8/E9 覆盖资源、异步竞态、诊断与测试。单文件负责数据库资源生命周期；不可变状态用于公开快照，私有 Session 子类填补建连前和 shielded close 期间的真实 Owner 缺口，实例日志过滤器解决池内部吞异常并日志化的凭证风险。没有通用资源框架、后台重连或业务事务重试。官方参照归 ADR，实际消费/接口归配置及基础设施正文。

**边缘与限制**：测试覆盖未启动 close、缺驱动、真实驱动连接拒绝、认证/权限/schema 分类、非法版本、未知错误、有限探针/清理/关闭、取消不合作、并发探针/关闭、父级预算、不释放外部 Owner、重复关闭、缓存工厂及迟到结果。真实 Session 测试不连接数据库，fake 只证明控制流；没有真实 PostgreSQL/schema/CRUD 成功证据，DB-F06 保持未完成。现有 5 个 strict xfail 仍由 F03/F05 闭合。

**交接**：F03/04 提供受信只读 schema_check，必须核对完整历史/head/结构/权限，并区分探针主动设置的只读事务与账号默认只读。F05 从 Settings 选择 runtime 参数（不全量展开含迁移预算的 database_config），先 drain 后以同一绝对 deadline dispose；首次启动未装配消费者仍要求重启。DB-F02 不改变现有应用可用性状态。

**文档拆分**：DB-F02 的组件内部契约（准入状态机、探测与关闭流程、资源责任、并发与取消边界、原因码分类）移至 `docs/infrastructure_doc/database_doc/database.md`；层文档 infrastructure.md 只保留定位、模块地图、容器直管现状与待接线边界。ALIGNMENT 的 database.py 条目与 docs/catalog.md 同步指向新文档。

**提交前审查**：审查级校验另发现三项未修复缺陷，见下节；本片暂不修复也不提交。

<a id="db-f02-open-findings"></a>

### DB-F02 提交前审查发现（2026-09-22，三项已全部修复）

结论：实现与文档声明基本自洽，todo 已记录的验证数字经逐条复跑全部对上（定向 115 passed、全量 1627 passed + 5 xfailed、ruff 与 verify_alignment 通过、除测试外无 `DatabaseRuntime` 引用）。下列三项为审查新发现，前两项代码缺陷均有复现证据（临时脚本仅在外部边界注入异常或吞取消，其余走真实代码路径，脚本未落盘）。未发现 G0-3 双重结算或迟到结果重新开放准入的路径。

- [x] P2：半构建引擎会开放准入并丢失池事件跟踪与日志脱敏 —— 已修复，见 [DB-007](../issues/infrastructure/database/2026-09-23-half-built-engine-admission.md)
- [x] P3：运行时自身的原因码被 `_reason` 归类为 internal_error —— 已修复，见 [DB-005](../issues/infrastructure/database/2026-09-23-runtime-reason-classification.md)
- [x] P3：`ping()` 在内部缺陷时会写入准入状态，与文档表述冲突 —— 已修复，见 [DB-004](../issues/infrastructure/database/2026-09-23-ping-admission-write.md)

| 严重度 | 位置 | 触发条件 | 后果 | 修复方向 |
| --- | --- | --- | --- | --- |
| P2 | [database.py:184](../app/infrastructure/database.py#L184) 先发布 `_engine`，监听器与 maker 到 187-192 才赋值 | `_build_engine` 中 184 行之后、192 行之前发生任何异常（三次 `event.listen`、logger filter 循环或 `async_sessionmaker` 构造） | 首次 `init()` 抛 internal_error 但 engine 已保留；再次 `init()`/`probe()` 置 ready 并放行工厂，而 `_maker` 仍为 None → 工厂调用抛 `TypeError: 'NoneType' object is not callable`。该次运行的 `_drivers`/`_borrowed` 永不登记（dispose 无法感知借出连接），实例日志过滤器也不存在，config.md 新写的「runtime 已脱敏 SQLAlchemy/驱动异常与 echo」在此路径不成立 | 用局部变量构造、成功后一次性赋值三成员，或失败时复位 `_engine`，使「engine 存在」等价于「构建完成」；补一条半构建回归测试。已按前者修复，见 [DB-007](../issues/infrastructure/database/2026-09-23-half-built-engine-admission.md) |
| P3 | [database.py:60](../app/infrastructure/database.py#L60) 的 `safe_reasons` 仅 3 码；[147-148](../app/infrastructure/database.py#L147-L148) 与 [162-164](../app/infrastructure/database.py#L162-L164) 抛自身原因码 | 探针任务在 `_probe_stopped` 置位后仍继续并再次 checkout（吞取消的驱动操作） | ADR D5 白名单内的 `closing`/`close_incomplete` 被判为未知程序缺陷，`probe()`/`init()` 抛 `DatabaseRuntimeError("internal_error")` 而非返回状态；终态仍 fail-closed，不改变准入结果 | 把运行期自身抛出的稳定码加入安全集，或按码值直接分类，避免白名单往返。已按前者修复，见 [DB-005](../issues/infrastructure/database/2026-09-23-runtime-reason-classification.md) |
| P3 | [database.py:209-212](../app/infrastructure/database.py#L209-L212) 与 [347-349](../app/infrastructure/database.py#L347-L349) | `ping()`（`_run_probe(full=False)`）命中 internal_error | 状态由 ready 变为 unavailable/internal_error；方向保守（不放开准入），但与 [DB-ADR-001 D4](../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d4运行时与故障语义)「`ping()` 只检查真实连接和最小事务，不改变能力准入状态」及 DB-003「ping 不更新健康快照」的表述冲突 | 非 full 探针只返回原因码，或把文档措辞改为「内部缺陷会关闭准入」。已按前者修复，见 [DB-004](../issues/infrastructure/database/2026-09-23-ping-admission-write.md) |

<a id="db-f02-followups"></a>

### DB-F02 后续发现（2026-09-23，两项已修复）

本轮实现核对（DB-004～DB-007）新登记两项，均为非阻断，不改变当前验收结论：

- [x] P3：`connect` 期的关闭拦截没有直接断言 —— 已修复，见 [DB-008](../issues/infrastructure/database/2026-09-23-connect-during-close-assertion.md)
- [x] P3：池实例 logger 名硬编码，换池实现会静默失效 —— 已修复，见 [DB-009](../issues/infrastructure/database/2026-09-23-pool-logger-name-hardcode.md)

| 严重度 | 位置 | 触发条件 | 后果 | 修复方向 |
| --- | --- | --- | --- | --- |
| P3 | `_connect` 的关闭期拦截（行为已登记在组件文档的行为边界） | 关闭已开始时连接池新建连接 | 该连接被 `terminate()` 并抛 `DatabaseRuntimeError("closing")`，此路径无直接断言；现有用例只覆盖“初始化期外部连接使 dispose 报 close_incomplete”，两者不是同一时序 | 用真实引擎并发关闭观察池在关闭期新建连接；fake 边界只能直接调用事件处理器，证明不了这个时序。已改为 fake 边界的直接断言（`closing` / `closed` 两态），并用“移除拦截分支”确认判别力；真实时序留 DB-F06，见 [DB-008](../issues/infrastructure/database/2026-09-23-connect-during-close-assertion.md) |
| P3 | `_build_engine` 的脱敏过滤器名 | `_engine_options` 出现 `poolclass`，或方言默认池变化 | 过滤器只挂在 `sqlalchemy.pool.impl.AsyncAdaptedQueuePool.<name>`：池实现一变，池事件日志不再脱敏，且不会有任何测试失败（echo 仍由 engine 实例 logger 覆盖） | 从 engine 实例读实际池 logger 名（`sync_engine.pool.logger.name`）而不是拼字符串，并补一条断言该名字已被过滤器覆盖的用例。已按此修复，见 [DB-009](../issues/infrastructure/database/2026-09-23-pool-logger-name-hardcode.md) |

<a id="c-02-lifecycle"></a>

## C-02：工具执行生命周期（交付 A 已完成，交付 B 待实施）

**目标**：从 ReAct 工具编排到 Integration 执行/清理/事实接管，再到领域协议历史和唯一终态，闭合取消、期限、有限重试及未知副作用后的准入。设计唯一正文为 [TOOLS-ADR-008](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)，不在此复制状态与恢复规则。

**授权边界**：用户已确认六项方向，并已逐 piece 授权和完成进程内交付 A。2026-09-22 已授权文档状态收敛及 Piece⑥数据库基础设施核验；本轮不安装驱动、不连接或迁移数据库、不创建账本实现。Piece⑥～⑧仍须按最新代码拆分并确认实施计划，不能把本次核验当作交付 B 的实现授权。

### 六项定案与交付依赖

六项决策及具体限制统一见 [ADR-008 Decision](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md#decision)：

- 分级持久化
- 分批验收
- code_exec 限制及演进
- 安全边界协作取消
- 独立事实收集＋选择性异常传播
- 单活动进程内多 Agent/多批次共享执行。

保留 P0～P5 编号便于追踪；编号不是必须串行完成的顺序：

- **交付 A（进程内）**：P0-A → P1 → P3-A → P4-A → P5-A。先以普通只读工具验收，不依赖完整执行账本，不承诺崩溃恢复。
- **交付 B（持久保护）**：P0-B → P2 → P3-B → P4-B → P5-B。按工具分级启用，可能有副作用/分类不明/强制调用审计的工具不得跳过必需意图记录和相关保护；复用 A 已完成的控制基础。
- P1 的“成功后处理失败不重放”可在相应修复方案确认后独立实施，不等待完整存储规格；重试安全策略变更仍依赖 P0-A 的适配器契约。
- A 完成只标进程内闭环；B 及相关真实资源/存储测试通过后才标对应持久保护完成，不提前改变未达标工具的保证。

上述箭头表示能力依赖，不要求分别提交不兼容的接口。P3-A 引入 Gateway 必填上下文时，必须同时完成 P4-A 中全部直接消费方、运行身份入口及测试替身的迁移，组成一个可测试的纵向变更；随后再分批扩充并行与终态验收。不以临时可空上下文维持旧调用。

### 本轮文档任务

- [x] 核实 ToolGateway、Executor、ReAct、适配器以及部署/持久化基础；研究结论直接纳入 ADR，不另建调研文件夹。
- [x] 主 ADR：完整生命周期、状态/竞态、逐工具能力、资源保护、恢复限制及工业参照。
- [x] 工具 ADR 索引与旧 004/006/007 承接指针：保留有效条款，标明迁移未完成。
- [x] 本计划：拆分文件职责、依赖顺序、验收与未授权候选。
- [x] lessons：记录“单次结果未知不等于全工具失效”的已纠正设计误判。
- [x] 文档链接、对齐和 diff 检查；实际结果见本轮评审记录。
- [x] 六项讨论定案回填 ADR/todo：撤销全调用强制意图落库及全量账本前置依赖，补多 Agent 隔离、选择性异常传播与未来升级路径。

### P0：最小端口与部署规格（已形成；Piece①～⑤已实施）

唯一详细规格见 [ADR P0 S1–S8](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md#tool-lifecycle-p0-spec)，初值见[配置规格](config_doc/config.md#tool-lifecycle-p0)，运行约束见[部署规格](project/deployment.md#tool-lifecycle-p0)。本节表格保留交付职责导航，不重复字段和参数值。

- [x] P0-A：公开签名、独立 facts 端口、共享异常出口、运行/批次/attempt 身份与状态迁移。
- [x] P0-A：Executor、Admission、Supervisor、BatchRunner/Collector 的方法及文件边界与 E9 依据。
- [x] P0-A：多 Agent 公平准入、实际消费者、同会话多 run 取消归属、禁止未支持嵌套执行。
- [x] P0-B：逐工具持久化/资源等级，保守目录范围与 code_exec 限制。
- [x] P0-B：SQLAlchemy 专用账本、CAS/事件幂等、SQL 迁移和 asyncpg 依赖接入位置。
- [x] P0-A/B：配置初值、Windows 单机 Owner、宿主关闭机制和验证矩阵。
- [x] P0 规格已通过实施评审；Piece①～⑤已按纵向切片落地并完成进程内只读交付 A，Piece⑥～⑧继续验收持久保护、副作用资源准入和跨重启恢复。

本阶段先补规格，不创建占位类。对现有消费者逐一映射：

P0-A 冻结运行/批次/调用身份、独立事实入口、类型化终止、父子取消与期限、共享容量和有界排队、协作检查点及只读能力验收。
P0-B 冻结持久化等级、账本迁移、恢复准入、资源身份和必要的启动排他/强制退出机制；
不能把全部 P0-B 作为只读交付 A 的前置条件。

| 文件/正式位置 | 工作内容 |
| --- | --- |
| `app/domain/ports/tool_gateway.py`（设计映射） | 确定控制参数、操作/尝试身份、结果及控制出口，必要事实字段与默认未知语义；保留既有调用次数口径 |
| `app/integration/tools/base.py`（设计映射） | 确定可信重试安全、资源范围、可选只读核验及执行结束证明的最小能力，避免为每个语义增加独立 Policy |
| ADR-008 | 补端口字段与退出形状、各运行独立事实收集、资源层级冲突表、逐工具持久化等级及 A/B 支持集合；不重开已定六项方向 |
| `docs/project/deployment.md`、`docs/config_doc/config.md`（规格确认后） | 明确单活动进程/存储范围、启动排他方式、schema 初始化与回滚；确定有界清理、接管容量、记录及核验重试配置，不复制多份默认值 |

上述原待冻结细节已在 ADR S1/S6/S7、配置与部署规格逐项给出明确方案。未来平台级升级仍按 D11/D12 触发，不能混入 P0 未完成项。初值尚无负载实测，DB/进程退出保证待 B 实施验证，不能把规格完成写成运行验收完成。

*部署核验结论*：目前只有本地启动证据，没有生产 worker/共享文件系统声明。首期支持单活动执行进程内多 Agent、多批次并发，实际共享资源统一准入，取消与事实按所属运行隔离；不把进程内锁声称为分布式保证。如目标必须多实例共享执行，先调整部署决策。Agent 收尾有界与整个应用退出有界分别验收。

*验收*：每个公开出口有返回/抛出及事实去向；每类工具有支持/限制结论；所有等待和后台工作有上限来源；架构依赖通过审查。

<a id="c-02-implementation-pieces"></a>

#### P0 规格的实施 piece

P0 S1～S8 是设计职责划分；下列 piece 是实施与验收单元，沿用 P1～P5 的任务归属。字段、状态及默认值以 ADR/配置规格为唯一正文；具体文件分工沿用下方对应 P 阶段，不在此复制。Piece①～⑤已兑现进程内只读交付 A，后续 piece 仍待实施；局部完成不代表整套生命周期闭环。

| Piece / 当前状态 | 范围与实施归属 | 主要规格依据 | 完成条件 |
| --- | --- | --- | --- |
| ① 调用前后处理与安全重试 / 已完成 | P1：已分离真实执行、结果处理和非关键观测；BaseTool 默认禁止重试，只有显式安全声明和次数余额同时满足才重复；见 [TOOLS-050](../issues/integration/tools/2026-09-14-executor-postprocessing-retry.md) | S2、S3、S8 | 红测闭合；工具相关 94 passed、全量 991 passed。生产适配器与 SDK 内部重试上界仍归 Piece ④逐项核验，不影响本 piece 的 Executor 边界验收 |
| ② 运行身份、控制信号与独立事实 / 已完成 | P3-A＋P4-A：已实现公开上下文、事实入口和类型化终止；Application 创建 run_id，贯穿 Gateway、Agent、策略及测试替身 | S1、S3、S4、S8 | 强制 call/facts 契约、同会话多 run 取消隔离、批次事实先接管和 shared 异常依赖方向已由专项与跨层测试覆盖；真实后台句柄由 Piece④补齐 |
| ③ 共享准入与配置迁移 / 已完成（进程内） | P3-A：已实现 ToolAdmission 的全局/单运行容量、有界排队、按运行轮转、可中断等待与撤回/关闭转换；同步配置字段、Container、settings.py 注释及配置文档 | S3、S5、配置规格 | 全局/单运行容量、队列上限、取消/deadline 竞态、撤回/关闭转换与放行同刻不泄漏、Permit 幂等释放、旧键迁移及 Executor 层准入拒绝出口已由定向测试覆盖；资源联合准入和跨进程 fencing 仍归后续 Piece⑦/⑧ |
| ④ 真实执行与有界清理接管 / 已完成（进程内） | P3-A：实现 Supervisor、AttemptHandle 及首批只读适配器的真实完成跟踪、清理和迟回事实接管 | S2、S3、S5、S6、S8 | 真实线程未结束不退容量；迟回值有 Owner；清理与接管有界；依赖不在仍被使用时提前关闭 |
| ⑤ 并行成果、协议历史与终态 / 已完成（进程内只读 A） | P4-A＋P5-A：在 ② 的事实接线上完善 BatchRunner/Collector、ReAct 历史及事件提交 | S1、S4、S8 | 部分完成后控制异常保留兄弟成果并类型化上抛；协议消息正确配对；正常终态 done 唯一，异常由上层处理；终态后无新业务调用 |
| ⑥ 持久意图、事实账本与恢复 / 待实施 | P2：实现存储端口、数据库适配器、迁移和恢复接线；覆盖条件更新、事件幂等、意图比较及累计核验次数 | S3、S7、S8 | 必需意图写入失败时零执行；旧事实不覆盖新事实；重启不重放业务、不重置核验次数；通过真实数据库验证 |
| ⑦ 副作用工具与资源保护 / 待实施 | P3-B＋P4-B：逐个接入文件、HTTP、code_exec 的持久记录、效果声明和资源保护，保留各适配器能力限制 | S2、S5、S6、S7、S8 | 部分/未知效果如实记录；冲突调用被阻止，无关资源可继续；未达到持久化与部署前提的工具仍禁止启用 |
| ⑧ 启动恢复、宿主退出与整体验收 / 待实施 | P5-B，配合 P2/P3-B：完成 Windows Owner、受支持宿主及真实进程验证；同步组件、配置、部署与总验收证据 | S3、S7、S8、部署规格 | 启动先恢复保护再开放；强退与重启边界经真实验证；数据库、线程/进程及全量回归通过后，才确认 B 的支持范围 |

建议执行顺序：① → ② → ③ → ④ → ⑤（交付 A）→ ⑥ → ⑦ → ⑧（交付 B）。这是验收推进顺序，不允许以未实现的依赖或空壳替代可运行接线：② 的新签名与消费者必须在一个可测试的纵向变更中完成；⑦ 可在受控测试中接线，但副作用工具正式启用须满足 ⑧ 所验收的启动/部署条件，必要前置实现随 ⑥/⑦ 同批完成。

每个 piece 开始时核对最新代码，按既有 P 阶段文件表拆成小任务；缺陷先写红测，新增组件必须接上真实消费者。完成后更新本表状态并链接实际验证/Issue，不把代码已写等同于验收通过。沙箱、分布式准入及其他可选能力仍按“可选项与未授权范围”管理，不随 piece 自动扩展。

### P1：建立失败复现并分离执行与后处理

先红测，之后仅按已批准契约修改；后处理异常范围修复与安全重试策略分成小任务，后者依赖 P0-A，前者不等待 P0-B/P2：

| 文件组 | 修改目的 |
| --- | --- |
| `tests/unit/test_tool_executor_components.py`、`test_tool_hooks.py`、`test_tool_audit.py` | 复现成功后截断/统计/审计异常不得再次执行；普通失败与控制终止分道；挂起观测有界 |
| `app/integration/tools/executor.py` | 真实调用与结果处理的异常范围分离；保留成功/部分事实；按安全声明限制尝试 |
| `app/integration/tools/hooks.py`、`security.py`、`result_processor.py`（按实际触点拆小提交） | 盘点 Hook 的业务性质；非关键观测有界隔离；展示裁剪不覆盖恢复事实 |
| `issues/integration/tools/`及所属索引 | 红测证实后按独立根因登记，不预先标已修复或一次生成所有 Issue |

验收：SDK/工具执行计数不因后处理失败增加；0/1/N 计数保持明确；永久错误无盲重试；无观测异常覆盖已接管结果。

**复审遗留（已修复，2026-09-19）**：适配器返回非 `ToolResult`、或返回 `ToolResult` 但字段越界时，`ToolExecutor` 现在都在真实调用返回边界统一收敛为 `ErrorCode.UNKNOWN` 的失败结果，效果状态为 `UNKNOWN`，发布终局事实且不进入重试；不再让 `AttributeError` / `TypeError` 逃逸并中断 ReAct 工具批次，也不再让非布尔 `success` 被真值判定当作业务成功。检查范围按下游消费方式确定（见 [executor 文档](integration_doc/tools_doc/executor.md)）。新增回归覆盖非 ToolResult 与非法的五种字段（content / success / error / error_code / effect_state），各断言单次执行、终局事实与禁止重试。真实线程/进程迟回值仍归 Piece ④。

### P2：持久事实垂直切片与装配

属于交付 B，依赖 P0-B；不是普通只读交付 A 的前置条件。按 D8 分级接入，既有证据保存要求独立核验，不以无需意图落库推导结果可丢弃。

#### Piece⑥实施前数据库基础设施核验（2026-09-22）

> **后继独立任务**：用户已要求将共享数据库建设从 Piece⑥分离，现由已批准的 [DB-F 计划](#db-foundation)承接。下文保留当时核验结论；其中“不顺带重构 database.py”仅约束当时工具任务，工具专属迁移路径已由 [DB-ADR-001](../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#database-supersession) 部分替代，底座代码仍待实施。

本轮只核验现状并收敛实施边界，没有安装驱动、连接数据库或创建账本文件。

**可以复用的基础**：项目已依赖 SQLAlchemy 2.0.51；`models/database/base.py` 提供共享 `Base`，Session/Message ORM 已使用该基类；Container 已持有 `AsyncEngine`、创建 `async_sessionmaker`，并在工具服务关闭后释放 engine。Piece⑥沿用现有 `database_url`、连接池配置、共享 `Base` 和 Container 生命周期，不顺带重构空的 `infrastructure/database.py`。

**已证实的缺口**：

- `pyproject.toml` / `uv.lock` 没有 `asyncpg`；当前环境 `find_spec("asyncpg")` 为 `None`，按默认 URL 构造 engine 实测抛出 `ModuleNotFoundError`。
- Container 只构造 engine，不执行连接或最小事务；“引擎创建成功”日志不能证明数据库、账号、schema 或权限可用。
- 初始化失败会把 `db_session_factory` 降级为 `None`，但 `SessionManager` 的数据库方法仍直接调用该工厂；应用和固定返回 `ok` 的健康端点可以存活，会话持久化能力却不可用。
- `scripts/init_db.py`、`scripts/migrate.py`、`models/database/tool_log.py` 均为 0 字节；`migrations/tools/`、工具账本模型、存储端口、存储适配器和 Owner 入口均不存在。
- 现有 Container / SessionManager 测试全部使用假 engine 或假 DB，没有 PostgreSQL 真实连接、迁移、事务回滚、CAS、重启扫描或权限测试。

**Piece⑥进入实现前的约束**：先补 `asyncpg` 和锁文件，再建立唯一的版本化 SQL 迁移入口；应用 startup 只做版本、连接和最小读写能力检查，不自动改 schema。账本存储必须复用 Container 的 session factory，并以真实 PostgreSQL 测试证明干净库升级、重复迁移、校验和拒绝、事务回滚、条件更新和事件幂等。数据库不可用时保持 A 能力与 B 能力分道，不能让固定健康响应或 A-only 启动掩盖 B 未就绪。

| 文件组 | 修改目的 |
| --- | --- |
| `app/domain/ports/`（P0 确定专用存储端口文件名） | 只定义实际需要的登记/条件更新/回执/未决读取能力，不建设通用 Repository 框架 |
| P0 已定位的新路径 | `domain/ports/tool_execution_store.py`、`infrastructure/tool_execution_store.py`、`infrastructure/models/database/tool_execution.py`；方法/表结构见 ADR S3/S7 |
| `app/infrastructure/models/database/`及专用存储适配器 | 实现操作/尝试账本与原子状态更新；不得以空 tool_log.py 已存在认定存储已实现 |
| schema 初始化/迁移入口（P0 定位后列实际路径） | 明确建表、升级、失败恢复及部署检查；不用现有会话表修改冒充工具存储 |
| P0 已定位的部署入口 | `scripts/migrate.py`、`scripts/init_db.py`、`migrations/tools/0001_execution_ledger.sql`、`infrastructure/tool_owner.py`、`scripts/run_tool_host.py`；此处 app 内路径省略 app/ 前缀，完整归属见 ADR S3。其中前两个当前是 0 字节占位文件，`migrations/tools/`、`app/infrastructure/tool_owner.py`、`scripts/run_tool_host.py` 均不存在——**占位文件不等于迁移入口已实现**。 |
| `app/container.py`、`app/integration/tools/tool_service.py` | 注入现有 session 工厂及账本；启动存储核验；接管生命周期在 DB 关闭前完成 |
| 存储单测及 `tests/integration/` 专项 | 验证重复写、旧事件乱序、意图/执行/结果各崩溃窗口、重启扫描，不仅 mock 整个存储端口 |

验收：必需意图写入失败时相关真实执行为零；不依赖该存储且准入满足的只读调用不被统一阻止；成功但必需记录失败只重试记录；并发条件更新不丢事实；重启不重放旧业务；未决记录不依赖内存存活。未完成真实数据库验证不得标记跨重启闭环。

### Piece④ 本轮实施拆分（2026-09-19，已授权）

沿用 ADR S2/S3/S5/S6/S8，不扩展 Piece⑤ 的领域批次提交或 Piece⑥～⑧ 的持久保护。命中 E1～E9；Supervisor 和 AttemptHandle 分别拥有独立的宿主接管与单次真实工作生命周期，Executor 保留准备、重试决策和结果处理。

- [x] `execution.py` 与直接测试：受控等待、真实线程句柄、接管容量预占、迟回值归属和有界关闭。
- [x] `executor.py` 与边界测试：准备/单次尝试/完成分段；每次 attempt 取得 Permit，真实完成才释放，退避可中断且不占槽；事实先接管再传播终止。
- [x] `base.py`、search/readFile/web_browse/RCA 与适配器测试：控制入口和可信只读声明；搜索及文件读取显式跟踪完整同步工作。
- [x] `settings.py`、Container、ToolService、loader 与装配测试：显式配置注入、固定实例、在途卸载保护和关闭未完成传播。
- [x] 更新工具组件说明、父导航、配置参考、ALIGNMENT 和本节评审；运行定向、全量、对齐与差异检查。

可选项保持后续归属：持久账本、资源冲突保护、子进程树强制终止和宿主退出看门狗。本轮不以线程取消或协程返回宣称这些能力已完成。

本轮验收补充：`test_react_strategy.py` 将同一场景分成内部类型化 deadline 与外层硬 timeout 两条测试，保留 Piece② 的选择性异常传播；没有提前实现 Piece⑤。`test_tool_fact_ownership.py` 改以真实句柄是否结束判定 cleanup，远端效果未知仍独立保留。异步观察器吞取消的修复见 [TOOLS-054](../issues/integration/tools/2026-09-19-observation-cancel-ownership.md)。

### P3：执行控制、真实资源接管与适配器能力

拆为 P3-A（依赖 P0-A/P1）和 P3-B（另依赖 P0-B/P2）。A 建设进程内控制、共享执行与普通只读接管；B 接入必要持久保护、写工具和重启恢复。A 可以独立验收，但不得冒充完整持久保护。

| 文件组 | 修改目的 |
| --- | --- |
| `tool_gateway.py`、`tool_service.py`、`executor.py` | 透传取消/绝对期限；刷新、审批、容量/资源等待、退避均受控；每次真实调用前复查 |
| `app/config/settings.py`、`app/container.py`、`docs/config_doc/config.md` / P3-A | 同批迁移旧键 agent_max_concurrent_tools 至 tool_max_concurrent_executions_per_run，不保留旧名兼容别名，并接入 tool_max_concurrent_executions 全局上限；同步修正 settings.py 字段注释及配置文档，明确单运行与共享全局容量的区别。验收同时核对字段、装配、注释和文档，不因改名单独扩大总并发 |
| `base.py`与最小控制/接管组件（P0 确定路径） | 等待竞态、任务/许可所有权、后台容量及有限恢复；不复用 LLM 私有异常 |
| `executor.py`、`tool_service.py`、`container.py`与 P0-A 确定的最小准入组件 | 多 Agent 共享全局/单运行容量、有界队列及简单按运行轮转；资源阻塞不长期占真实执行槽；各运行状态隔离；禁止未支持的网关递归执行 |
| `builtin/file_ops.py`、`builtin/code_exec.py` | 文件部分写入事实、跨工具资源层级、进程及后代结束边界；不能从 Shell 返回推定全部副作用结束 |
| `builtin/search.py`、`builtin/web_browse.py`、`external/http_api.py` | 线程实际结束前保留容量；对象失败不全工具隔离；HTTP 未知写按适配器契约保护 |
| `loader.py`、`tool_service.py`、`container.py` | 实例版本固定；卸载/关闭不抢占在途共享资源；启动恢复保护后才开放准入 |

每组独立红测与小提交；现有 `test_tools.py`、`test_tool_loader.py`、`test_http_api_tool.py`、`tests/integration/test_tool_execution.py` 按实际边界扩展。接管子组件新增测试文件的名称随 P0 结构确定，不预造空文件。

### P4：领域并行事实、协议历史与终态

### Piece⑤ 本轮实施拆分（2026-09-19，已授权）

沿用 TOOLS-ADR-008 的 S1/S4/S8 与交付 A：

- [x] `tool_batch.py`：批次预登记、逐项结果接管、控制信号下的兄弟任务协调与有界收尾；Collector 继续只保存事实。
- [x] `react.py`：调用 Runner；在事件 yield 前提交输入顺序的 tool 回执、证据与消息历史；工具控制信号保持类型化上抛，不再启动新业务调用。
- [x] 定向红测和真实 ToolService/Executor 回归：部分成功+取消/期限/run_stop、未执行/未知回执、硬超时、提前关闭、历史配对、正常 done 唯一；跨运行准入与取消隔离沿用 Piece②③回归。
- [x] 同步正式组件文档、对齐表与评审结论；运行定向、全量及对齐检查。

本轮不提前建设 Piece⑥～⑧ 的持久账本、资源联合准入或副作用工具安全启用。

P4-A 随 P3-A 验收只读并行闭环；P4-B 随 P3-B 补持久回执、未知副作用及恢复引用。采用独立事实收集＋选择性异常传播，不把业务终止统一转换成正常回执，也不强制在异常对象中复制全部批次成果。

文件工具安全放弃点、HTTP 未知写保护及 code_exec 必需意图记录随 B 验收。code_exec 限制与升级方向见 ADR D11；首期不默认实施沙箱、完整进程树隔离或任意命令资源推导，也不将这些能力标为已具备。

| 文件组 | 修改目的 |
| --- | --- |
| `app/domain/reasoning/react.py` | 每个完成调用立即接管；批次取消保留兄弟结果；主生成器统一事件；终态选择前归并事实 |
| `app/domain/reasoning/reflection.py`、`planner.py`及 Agent 消费者（按引用核实） | 透传同一控制边界、吸收已接管成果，不给子跑重置 deadline，不吞类型化终止 |
| 现有 Application 协作调用方及策略构造入口（P0-A 按引用定位） | 父取消向下传播、单运行取消不横向误伤；每次运行独立可变策略状态；不为本期新建协作任务拆分与汇总平台 |
| `tests/unit/test_react_strategy.py`、`test_react_strategy_nonstream.py`及相关策略套件 | 对真实 ToolService/Executor 边界做并行、协议与终态测试，不能只 mock 整个 Gateway 返回 |

验收：部分完成后任一路径均不丢事实；没有重复工具执行、重复结果或第二个 done；未执行/未知结果如实配对；下一请求 history 合法；终态后没有新业务调用。

### P5：总矩阵、文档与收口

P5-A 先验收进程内场景；P5-B 验收持久/恢复及受支持副作用工具，并做整体回归。下表按 A/B 实际支持范围选取，不能要求先跑完 B 才交付 A，也不能跳过 B 测试宣称持久能力就绪。

| 场景组 | 必须观察的行为 |
| --- | --- |
| 入口/刷新/审批/容量/锁/持久化/退避中 cancel 或 deadline | 实际 attempt 数；未执行事实；已取许可归还；无后台 waiter 泄漏 |
| 信号与许可/结果同时到达；任务吞取消迟回 | 值有 Owner；许可不丢；结果记录一次；控制终态不变 |
| attempt 内业务取消、局部超时、总期限、硬 Task.cancel | 当前 attempt 边界准确；期限不重置；不同超时来源不误分类 |
| 线程继续、进程未结束、清理超过窗口、队列已满 | 真实容量不提前释放；有界移交；新准入受限；原 Agent 可收尾 |
| 成功后解析/截断/日志/审计/事实写入失败 | 不再执行业务；成功事实不丢；关键记录与非关键观测分道 |
| 并行一成功一挂起、事件 yield 后消费者关闭 | 兄弟事实已接管；无重复 done；结果顺序和协议配对合法 |
| 多 Agent 多批次、一个运行取消或局部超时 | 全局/单运行并发符合上限；无串结果；无关 Agent 继续；父级取消向下覆盖 |
| 大批次持续到达、共享资源阻塞、排队满、嵌套执行 | 有界排队及公平进展；无关资源不被占槽拖住；溢出明确拒绝；无持槽等待子调用死锁 |
| 自有工具在写入前取消、不可拆操作中取消 | 安全边界停止未开始写入；已发生事实保留；不支持协作的工具按声明边界验收 |
| 未知写同资源/无关资源/跨工具/父子范围/路径别名 | 只阻止真实冲突；核验允许；不能靠改 call ID 绕过 |
| 操作成功/失败/部分完成/未知，随后自动或人工恢复 | 容量释放、冲突解除、业务确认独立；无盲目 replay 或补偿 |
| 意图前后/执行后/结果落库前崩溃、旧实例迟回 | 启动恢复保护；不重放；旧状态不覆盖新事实；schema 与排他边界真实验证 |
| 0/1/N、错误交替、SDK 嵌套重试 | 有可证明真实调用上界；安全条件不因预算存在被豁免 |

完成代码后更新工具/端口/ReAct/存储组件说明、配置、部署、ALIGNMENT 和相应 Issue；只同步已兑现的行为。ADR 标记实施证据，旧决策指针按实际迁移状态更新。根级 ADR-003 的通用顺序不重复建正文。

验证命令以 [部署说明](project/deployment.md) 为正式入口。按批次运行工具执行、Hooks、审计、HTTP、资源、ReAct/策略定向测试；最终全量 pytest、真实数据库恢复与受控线程/进程验证、`scripts.verify_alignment`、`git diff --check`。没有服务/平台证据时如实列未验证项，不能用单测数量替代端到端验收。

### 可选项与未授权范围

- 多实例共享资源的分布式准入与 fencing：真实部署需要时先补决策，首期不宣称支持。
- 任意工具跨调用业务语义去重、自动补偿/重放、通用工作流恢复引擎：不在本期。
- 通用工具熔断、管理 UI、插件平台扩建：有产品或故障证据后再规划；本期必要核验/人工记录能力不等于建设管理平台。
- 工具费用精确预留与通用 SDK 自动重试治理：仅核验本次触达工具的真实尝试上界，不扩大到全仓。
- code_exec 沙箱、平台进程树管理、网络出口及持久输出通道，租户公平调度、跨机器执行服务、持久工作流：需求触发与具体限制由 ADR D11/D12 唯一维护；不纳入本期必须完成清单。

### 2026-09-19 审查缺陷修复（已完成）

用户已授权；命中 E3/E5/E7/E8，沿用现有 Owner 和文件级同步注销，不增加抽象。

- [x] `execution.py` / `test_tool_attempt_lifecycle.py`：6 个线程证据丢失场景先红后绿；保留未交付线程记录，真实完成释放 Permit。
- [x] `loader.py` / `test_tool_loader.py`：兄弟工具调用竞态先红后绿；首个 await 前注销整个文件的工具。
- [x] 同步组件说明、Issue 索引及 lessons；相关执行/接管 34 项、加载器 29 项通过，全量 1360 passed（48.96 秒，1 个既有弃用警告）；本次四个 Python 文件 Ruff、对齐与差异检查通过。

评审：正常线程成功/异常均无记录与容量泄漏；取消、硬取消和超时保留未交付线程值及异常。问题详情见 [TOOLS-055](../issues/integration/tools/2026-09-19-thread-cleanup-evidence.md)、[TOOLS-056](../issues/integration/tools/2026-09-19-plugin-file-unload-race.md)。可选的自动消费恢复记录、持久恢复与新结果解释协议未纳入本轮。

### 本轮评审记录

2026-09-19 最新结论：Piece①～⑤已完成进程内只读交付 A。Piece⑤加入 ToolBatchRunner：先预登记，再逐项接管并行结果；控制异常给兄弟有界收尾机会。ReAct 将 assistant.tool_calls 与全部真实/未执行/未知回执在首个工具事件前一起提交，提前关闭不留下半配对历史；final_answer 与普通工具混用在写历史前拒绝。三类运行级工具控制异常仍类型化上抛，正常终态的 done 不重复；聊天 SSE 的异常出口继续由 Application 统一收口。Piece④ 的真实任务、线程和迟回值 Owner 与已完成验证见上文；持久消费/恢复仍未建立。

验证：真实线程/取消/deadline/硬取消、迟回值、清理移交、关闭依赖、退避释放容量、未确认事实容量封顶、审批及观测边界已通过定向测试；最新三个执行/事实套件 40 项通过。最终全量 1353 项通过（47.35 秒），包含重试后准入拒绝保留实际次数回归。对齐与差异检查通过。使用 `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Users/Administrator/AppData/Local/Temp/agent-forge-piece4-tests`；Ruff 受影响核心文件通过。全改动文件 Ruff 扫描另报告存量 settings/test_react 的长注释、旧测试未使用 noqa 和未标 ClassVar 的类属性；未扩大清理范围。Starlette/httpx 弃用提示不在本次业务改动内。

Piece⑤ 验证：批次/协议/嵌套策略定向 171 passed；全量 1475 passed；`verify_alignment` 与 `git diff --check` 通过（2026-09-20 复核并修复「回执选取在途重试」与「硬取消重取宽限」两项后的实测值；修复前为 169 / 1440）。Piece⑥～⑧仍待实施：当前写工具/未知插件不能被宣称具备 B 级保护；正式执行及模型导出的启用门禁已落实（见[完成记录](history/completed-work.md)），B 完成前拒绝副作用、未知效果和强制审计能力。目录级资源联合准入、持久记录/恢复、子进程树与宿主强退均未实现。

2026-09-20 D4 修复（畸形调用结构）：先在单元层（`action_fingerprint` 对缺 `function` 的调用）与策略层（脚本化 LLM 返回 `[{"id": "call_1"}]`）各建失败复现，堆栈确认路径为 `_bump_stall → action_fingerprint → KeyError: 'function'`，outcome 记 `Agent 运行异常: KeyError`。修复后两处转绿，畸形调用按「工具 `unknown` 未注册」失败回喂并进入下一轮。验证：定向 `test_react_protocol.py` + `test_react_strategy.py` 160 passed；全量 1477 passed（52.40 秒，1 条既有 Starlette/httpx 弃用警告）；`verify_alignment` 与 `git diff --check` 通过；Ruff 受影响文件与格式检查通过。

2026-09-21 D1+D2 修复（批次宽限配置化与操作事实归属）：D1 新增配置键 `tool_batch_cleanup_grace_seconds`（有限正数，默认 1.0），经 `settings → container.agent_params → AgentContext → ExecutionLimits → execute → _handle_tool_calls → execute_tool_calls → ToolBatchRunner.run` 注入，模块常量降级为 `DEFAULT_BATCH_CLEANUP_GRACE` 兜底；D2 把回执的操作事实归属从 `next()` 插入序改为显式规则（带结果的优先、同为带结果取最后插入）。新增三项测试：runner 级注入生效（0.15 秒 → 整批 <0.6 秒）、策略级四层透传（同场景 0.16 秒完成，任一层漏传会退回 1 秒）、双操作事实归属的两条规则。验证：全量 1483 passed（51.78 秒，1 条既有 Starlette/httpx 弃用警告）；`verify_alignment`、`git diff --check`、Ruff 与格式检查通过。新增配置键须同步测试替身——首轮全量暴露 17 项 `KeyError: 'batch_cleanup_grace'` 与容器断言失败，补齐 `tests/conftest.py` 共享 `agent_params` 与 `test_container.py` 的完整装配断言后全绿；该失败面正是「新增 AgentContext 字段必须同时更新替身形状」的既有约定。

历史背景见 [C-02 文档设计交接](history/completed-work.md#c-02-p0-design-history)，不在活动计划中重复各轮验证叙述。

<a id="l-01-domain-layer"></a>

## L-01：领域层推理决策架构完整建设（Slice 0/3/4 已完成；1/2/5/6 部分）

日期：2026-08-27（建设起始）。目标：在应用层多任务编排（Phase C）之前，把领域层四模块建设完整并跑通单 Agent 任务——策略库 `reasoning/`、Prompt 管理 `prompts/`、记忆模块 `memory/`、Agent 编排 `agent/`。产品锚点：`PlannerAgent.plan()` 是主 Agent 拆分原语（Phase C 主链路第一步），`ReActStrategy` 是子 Agent 排查引擎复用，Reflection 承担证据链语义自查，Memory 是历史排查经验的领域前置。

**授权边界**：本节的执行依据是 2026-08-27 会话记录；原独立计划文件已删除，设计正文与进度并入本节。Slice 0/3/4 已落地；**未完成项尚未重新获得执行授权**，逐项实施前按[项目工作流](engineering/project-workflow.md#planning)重新确认范围与必要性——不因本表存在自动开工，也不因已有计划自动创建端口、空壳或存储。

已完成部分的设计与验收由正式位置承载，本节不复制：组件说明见 [react.md](domain_doc/reasoning_doc/react.md) · [reflection.md](domain_doc/reasoning_doc/reflection.md) · [planner.md](domain_doc/reasoning_doc/planner.md)；决策见 [ReAct 抽离](../adr/domain/agent/2026-08-27-react-strategy-extraction.md) · [Reflection 策略](../adr/domain/reasoning/2026-08-31-reflection-strategy.md) · [Planner 两层结构](../adr/domain/reasoning/2026-09-05-planner-strategy.md) · [结构化输出](../adr/domain/reasoning/2026-08-28-structured-output.md)。

### 垂直切片与状态

| Slice / 状态 | 内容 | 关键文件 | 当前证据（2026-09-21 核对） |
| --- | --- | --- | --- |
| 0 ReAct 抽离 / 已完成 | ReActStrategy 独立成类，构造注入 llm + tools，不 import agent/ | `reasoning/react.py` | 超计划：错误分发 13 kind、各护栏全阶段、stream_mode 双通道、收尾契约化 |
| 1 Prompts 完整化 / 部分 | 模板完整化 + 三个 builder + 默认 system 注入 | `prompts/templates/planning.py` · `templates/reflection.py` · `prompts/manager.py` · `agent/base.py` | 模板与 builder 已完成（`planning.py` 63 行、`test_prompts.py` 392 行）；`run()` system 注入未做 |
| 2 Memory 基座 / 未开始 | 端口 + 骨架，向量检索留 Phase D | `ports/vector_store_port.py` · `memory/` 五个模块 · `test_memory.py` | 端口文件不存在；`memory/` 五个模块文件（另 `__init__.py`，共 6 个）与 `test_memory.py` 均为 0 行 |
| 3 PlannerAgent / 已完成 | 两层结构，每步复用 `ReActStrategy.execute` | `reasoning/planner.py` · `agent/planner.py` | 超计划：replan 循环、PLAN_FAILED 分发、usage/done 口径、cancel 与成本护栏全阶段 |
| 4 ReflectionAgent / 已完成 | 收集 → 初稿 → 自查 → 修正 | `reasoning/reflection.py` · `agent/reflection.py` | 实际落为 `agent/reflection.py`（原计划写 `agent/reasoning.py`）；超计划：cancel/超时护栏、explicit_abstention、usage/cost 全阶段 |
| 5 装配接线 / 部分 | 记忆装配 + 标注放宽 + 重导出 | `container.py` · `api/deps.py` · `chat_service.py` · `task_service.py` · `agent/__init__.py` | 重导出已完成；容器/依赖/聊天三处均无 `MemoryService` 接线；`task_service` 标注仍为 `ReActAgent` |
| 6 文档与 ADR / 部分 | 模块文档 + 四个 ADR + 对齐 | `ALIGNMENT` · 各模块文档 · `adr/domain/` | React/Reflection/Planner 文档与 ADR 齐；记忆分层与 CoT 的决策未留档 |

### 未完成项

- [ ] Slice 1：`BaseAgent.run()` 顶部在无 system 角色消息时注入 `PromptManager.build_system_prompt()`，并补对应测试。触发：先确认产品是否需要默认注入，避免改变已有系统提示词优先级。
- [ ] Slice 2：记忆基座——新建 `ports/vector_store_port.py`，实现 `memory/` 五个模块，填空 `test_memory.py`。范围限定为端口 + 骨架，不 mock 向量库。触发：出现实际的跨会话记忆消费方。
- [ ] Slice 5：记忆生产装配——`container.initialize` 组装三层并注入聊天路径；`api/deps.py` 提供可返回 None 的 provider；`memory_enabled` 默认 False，保持惰性零行为变化。依赖 Slice 2。
- [ ] Slice 5 残留：`task_service` 的 `agent` 标注由 `ReActAgent` 放宽为 `BaseAgent`（无行为影响，标注比实际窄）。
- [ ] Slice 6：记忆分层与 CoT 预留的决策留档——原计划要求 `memory-layered-contract` 与 `cot-upgrade-path` 两个 ADR，当前均不存在，决策依据只剩本节与模块文档的「预留」章节，且未写升级触发条件。

### 设计要点（未完成部分）

**Slice 1 · 默认 system 注入**：`BaseAgent.run()` 顶部检查 `messages` 是否已有 `system` 角色消息；无则注入 `PromptManager.build_system_prompt(tool_descriptions)`。现有调用方（ChatService）自行组装 messages，注入点必须避免重复或覆盖，这也是它至今未做的原因——先确认产品是否需要默认注入。

**Slice 2 · 记忆模块**（端口 + 骨架，向量检索留 Phase D）：

- `ports/vector_store_port.py`（新建）：`VectorStorePort(Protocol)`（`upsert` / `search` / `delete`）+ `VectorHit`；登记 `ports/__init__.py`。
- `memory/base.py`：`MemoryItem` + `MemoryBackend(ABC)`（`add` / `get` / `remove` / `clear`）。
- `memory/working.py`：`WorkingMemory`（dict 承载 + `snapshot`）。
- `memory/short_term.py`：`ShortTermMemory`（有序 dict，`max_size` 满则逐出最旧 + `list_recent`）。
- `memory/long_term.py`：`LongTermMemory`，构造注入 `vector_store: VectorStorePort | None` + `embedder: EmbeddingPort | None`；任一为 None → no-op（骨架而非 mock，同 `SessionManager(redis=None)` 降级范式）。
- `memory/memory_service.py`：`MemoryService` 聚合三层（working / short_term / long_term / enabled），`enabled=False` 全惰性；签名只用原始类型，不 import `agent/`。
- `agent/base.py`：`BaseAgent.__init__(llm, tools, memory: MemoryService | None = None)`（向后兼容）；`run()` 结束记录 `_memory.record_task(...)`，**异常吞掉不阻断任务**——记忆写入不得影响任务终态。

**Slice 5 · 装配接线**：`container.initialize` 组装四件（`WorkingMemory` + `ShortTermMemory` + `LongTermMemory(vector_store=None, embedder=embedding_service)` + `MemoryService(enabled=settings.memory_enabled)`）；`api/deps.py` 提供可返回 None 的 `get_memory_service()`（不 raise）；聊天路径把 `memory_service` 传入 Agent。`memory_enabled` 默认 False ⇒ 惰性零行为变化。

### 测试计划（未完成部分）

- `test_memory.py`（现为空文件）：Working / ShortTerm 窗口逐出 / LongTerm 委托与 `None` no-op / MemoryService 惰性（`enabled=False` 时不触达后端）。
- `test_prompts.py`：补默认 system 注入用例（builder 冒烟已存在）。
- `test_container.py`：补 `memory_service` 的惰性装配断言。
- 沿用原计划约定：**全手写假对象 + monkeypatch，不用 AsyncMock**。

### 已完成项

- [x] Slice 0：ReAct 抽离（含错误分发、护栏、双通道、收尾契约化等超计划部分）。
- [x] Slice 1 的模板与 builder：`templates/planning.py` 完整化、`templates/reflection.py` 新建、`build_planning_prompt` / `build_reflection_prompt` / `build_self_check_prompt` 三个 staticmethod、`test_prompts.py`。
- [x] Slice 3：PlannerAgent 两层结构（策略在 `reasoning/planner.py`，桥接在 `agent/planner.py`）。
- [x] Slice 4：ReflectionAgent。
- [x] Slice 5 的 `agent/__init__.py` 重导出 `PlannerAgent` / `ReflectionAgent`。
- [x] Slice 6 的 React / Reflection / Planner 模块文档与 ADR；`structured-degradation-contract` 的决策由[结构化输出 ADR](../adr/domain/reasoning/2026-08-28-structured-output.md) 承接（无同名文件）。

### 原计划的决策与已废弃项

- **已废弃**：「`test_agent.py` 零改动依赖 `ReActAgent._execute_tool_calls` 转发方法」——该转发已在 [AGENT-001](../issues/domain/agent/2026-09-16-base-contract-drift.md) 删除（只有两项重复测试调用它），原决策不再成立。
- **已被修正**：原计划「PlannerAgent 执行阶段复用裸 `execute_tool_calls`」由 [Planner 两层结构 ADR](../adr/domain/reasoning/2026-09-05-planner-strategy.md) 改为「每步复用 `ReActStrategy.execute`」；原计划「PlannerAgent 整体归 `agent/`」同样被该 ADR 修正为两层结构。
- **仍然有效**：记忆模块不 mock 向量库，以 `VectorStorePort` 依赖倒置，`None` 时 no-op；`generate_structured` 返回 None 的降级链（plan None → ReAct 兜底；summarize None → 纯文本；critique None → 采用初稿）已由[结构化输出 ADR](../adr/domain/reasoning/2026-08-28-structured-output.md) 与各策略文档承载。

### 边界与不采纳

- **CoT 保持预留**：`reasoning/chain_of_thought.py` 为零字节预留空壳——既无实现，也未记录升级路径；本期不写实现。状态以[对齐表](ALIGNMENT.md)与 [reasoning 说明的 CoT 预留](domain_doc/reasoning_doc/reasoning.md#cot-预留)为准。
- **记忆的向量检索留 Phase D**：不 mock 向量库，以 `VectorStorePort` 依赖倒置；`app/integration/vector_store/` 现为空壳。
- 原计划的 `agent/reasoning.py` 未创建，Reflection 桥接实际落在 `agent/reflection.py`；以代码为准。
- 本节的未完成项不等同于承诺排期；触发条件写在对应条目内，不另立候选副本。

### 评审

2026-09-21 对照原计划与当前代码逐项核对：`planning.py`（63 行）与 `test_prompts.py`（392 行）说明 Slice 1 的模板/builder/test_prompts 已完成，计划原文的「待做」已过期；`base.py` 与 `chat_service.py` 均无 `PromptManager` 引用，`run()` system 注入确未做；`app/domain/memory/` 五个文件与 `test_memory.py` 均为 0 行、`ports/vector_store_port.py` 不存在，Slice 2 确未开始；`container.py` / `deps.py` / `chat_service.py` 无 `MemoryService` 接线，而 `agent/__init__.py` 已重导出两者。原计划的四个 ADR 中 `react-strategy-extraction` 已存在、`structured-degradation-contract` 由结构化输出 ADR 承接，`memory-layered-contract` 与 `cot-upgrade-path` 缺失。同日按用户要求删除原独立计划文件并把设计正文并入本节，原候选 C-06 一并被本节接管（已从候选表移出），C-07 收窄为 checkpoint 与 trace。本次只做计划整合与状态核对，未修改产品代码。核验另修正一处边界表述：`chain_of_thought.py` 实为 0 字节空壳，原文「只记录升级路径」不成立。

<a id="candidates"></a>

## 独立边界与候选建设

以下均需先重新确认必要性和执行范围；目前没有在实施的代码任务。候选不是必须实现清单。本表同时收敛各 ADR 正文记录的升级路径与未决决策；已随 R-01（见[完成记录](history/completed-work.md#refactoring-plan)）、C-02、L-01、[B-01](history/completed-work.md#candidate-closeout-01) 完成或已由它们承接的项不在此重复。

| ID | 候选 / 待核验边界 | 保留理由与触发条件 |
| --- | --- | --- |
| C-01 | Planner `REPLAN_SCHEMA.steps` 的 `maxItems` 边界。 | 2026-09-10“三策略重试上限闭环”收尾明确未并入。检查当前 schema 与实际输出规模，确认风险后独立处理；不与协议重试预算混为同一根因。 |
| C-03 | LLM 重试配置的非负校验。 | 同一最新收尾明确未并入。先核实配置、Manager 与调用入口；不按参数名统一 Tool/LLM 的不同计数口径。 |
| C-07 | 循环 checkpoint 与运行轨迹持久化。 | [checkpoint ADR](../adr/domain/reasoning/2026-08-30-checkpointer-resume.md)明确未实现；[trace ADR](../adr/domain/trace/2026-08-31-trace-persistence.md)为已决策待实施。出现实际消费方后分别确认范围，不因已有计划自动创建端口、空壳或存储。记忆基座、生产装配与 CoT 预留的进度与边界见 [L-01](#l-01-domain-layer)。 |
| C-08 | LLM 精确 provider 计数、上下文语义摘要、调用前成本预留与供应商共享配额映射。 | **三项未做**（2026-09-21 核验）：精确 provider 计量（`token_counter` 全用 tiktoken，`llm_service` 计数恒取 `main` 的编码器而与 model_key 无关，`request_budget` 自述不伪装成 provider 精确计量）、上下文语义摘要（`context_manager._truncate_messages` 直接丢弃历史，与 C-16 是同一处）、供应商级共享配额（`ReservationLimiterManager` 按 model_key 分桶，无 vendor 共享；四个 model key 共用同一 `base_url`/`api_key`，同一账户被拆成四个桶，是否真越界取决于 provider 按账户还是按模型限速——**待核验**）。<br>**「调用前成本预留已做」的旧表述已撤销**：`reservation_limiter` 的 `reserve`/`reserve_adaptive` 预留的是 **TPM 限流配额**，与成本预留不是同一件事；成本侧至今只有调用后累计，那条缺口记在 C-18。分别以容量利用率、证据可追溯性、严格成本或供应商真实配额需求触发，不套同一实现。 |
| C-09 | 配置扩展、默认值调优、热更新与多环境配置。 | 2026-08-29 config 文档重构留下研究性 backlog；没有真实消费方、负载或运维证据的默认值建议不保留为目标值。出现明确需求后重新设计，而不是照抄旧数值。 |
| C-10 | 工具选择器向量召回与工具加载/安全边界的后续增强。 | **表述已修正**（2026-09-21 核验）：`DefaultToolSelector.select` 是无条件全量返回的空壳接口，**没有任何选择逻辑**，所以这不是「增强现有选择器」而是从零建召回；且 `ToolSelector.select(tools)` 与 `ToolGateway.get_openai_tools()` 都不带查询入参，**现接口无处传查询向量**，实施前须先做接口与契约决策。当前 11 个注册、8 个可见（门禁过滤后），触发门槛「工具数 > 50」无证据。<br>[TOOLS-049](../issues/integration/tools/2026-08-20-code-review-fixes.md) 的 6 项性质是**已决策保持现状**（原文即「取舍项（保持现状）」），不是延后待做——两者触发判断不同，不能混为一谈。已完成的审计脱敏不重开。 |
| C-11 | 其他非关键观测入口的异常与阻塞边界。 | **终态/请求/收尾路径已实施**（2026-09-21，见 [B-01](history/completed-work.md#candidate-closeout-01)）：新增 `app/shared/observation.py` 的 `isolate_observation`，接入 react 终态、`dispatch` 降级告警、工具审计/观测/统计、流关闭、限流截断与上下文裁剪共 8 处。<br>**候选原述的靶子不成立**：`security.log_event_async("tool_call")` 的唯一生产调用点位于 `executor._observe` 的 `supervisor.wait` 之内，有界性与后台任务 Owner 由 Supervisor 边界提供，不重复设界。<br>**仍未做**：`metrics.py` 为 0 字节、指标入口不存在（无消费方，仅登记）；`structured.py`、`loader.py`、`client.py`、`assembler.py`、`hooks.py`、`session_manager.py` 等 except 分支内的降级告警未纳入；同步路径只能证明异常隔离、不能证明有界。对应 [ADR-002](../adr/2026-09-12-single-source-governance.md) 的「G0-6 的后续代码符合性工作」。 |
| C-16 | `build_messages` 的上下文摘要压缩（[ADR-001](../adr/2026-09-02-request-build-validation.md) docstring 策略第 4 条）：当前为硬丢弃历史，正文明确按产品导向暂不实现。 | **与 C-08 的「上下文语义摘要」是同一函数同一出处**（`context_manager._truncate_messages`），实施时应合并为一条，否则产出两份重复设计。另注该模块 docstring 声明「支持历史摘要压缩」而实现只做丢弃，属注释与实现不一致。触发：出现「丢弃历史导致答案质量下降」的真实证据；摘要须先证明不破坏证据链。 |
| C-17 | 结构化输出的产物链接入与兜底（[ADR](../adr/domain/reasoning/2026-08-28-structured-output.md)）：产物链接入时经 `AgentContext` 配置 `output_schema`；`final_answer` 失败叠加 `generate_structured` 兜底（当前未叠加）。 | 出现产物链消费方。 |
| C-18 | 成本上限的估算式 pre-call 软闸（[ADR](../adr/domain/reasoning/2026-08-30-cost-limit.md)）：发出前按输入上下文与 `max_tokens` 上界估算、超预算即拒绝，调用后按实际 usage 对账；需扩展 `CostLimiterPort`（现 `check` 为纯累计、无估算入参）。 | 出现「单轮绝不允许过贵」或并行子 Agent 各自发请求的真并发；工业参照 Portkey / llm0 / LiteLLM reservation。 |
| C-19 | 停滞检测的结果维度（[ADR](../adr/domain/reasoning/2026-08-30-stall-detection.md)）：① `result_hash` 防轮询误判（动作签名加结果 hash，须跨轮保存上轮结果）② 周期模式检测（A→B→A→B 滑动窗口，误杀风险高）。 | 出现真实的轮询或周期死循环误判；默认上限与参数规范化已降低概率。 |
| C-20 | OpenAI 异常归一的具名子类（[ADR](../adr/integration/llm/2026-09-01-openai-error-normalization.md)）：拆 `LLMUnauthorizedError` / `LLMForbiddenError`。 | 需要区分「key 过期」与「权限不足」时；当前 `status_code` 已够差异化，不预先拆分。 |
| C-21 | 半流续接的三类扩展（[ADR](../adr/integration/llm/2026-09-03-mid-stream-continuation.md)）：tool_call 半成品续接、reasoning 半段续写、SSE 传输游标续传（Vercel resume 式，客户端↔服务端课题）。 | 正文明确按产品导向暂不实现；传输游标续传属独立决策。 |
| C-22 | Application 语义预算作最终请求硬上限（[ADR](../adr/integration/llm/2026-09-06-request-context-budget.md)）：须复用 Integration 内部机制，不新增 `LLMGateway` 调用级参数。 | 确需把语义预算作为最终请求硬上限时；主/副模型共享配额合并记账见 [C-08](#candidates)。 |
| C-23 | 外部工具热加载的扩展项（[ADR](../adr/integration/tools/2026-08-17-external-tool-hot-reload.md)）：后台轮询、原子无损（引用计数 + 版本化实例）、元数据与实现分离 + 懒加载、多版本灰度回滚、沙箱隔离（子进程 / WASM / Sidecar）、`health_check` 自动巡检。 | 分别触发：重载窗口不可接受 / 工具数达数百 / 出现不可信第三方 / 插件数量上升。 |
| C-24 | 异常体系的两处收敛（[ADR](../adr/shared/exceptions/2026-08-28-exception-system-optimization.md)）：`classify_error` 识别 `AppError` 树（可重试判定收敛单一事实源）；`AgentRunError` 的 kind → 对外业务码映射。 | **A 半场已实施**（2026-09-21，见 [B-01](history/completed-work.md#candidate-closeout-01)）：`classify_error` 在 `status_code` 判定之后显式识别 `AppError` 树，零行为变化。判据不得提到函数开头——`LLMAPIError` 继承 `NonRetryableError` 但靠 `status_code` 决定可重试性，提前会让 `LLMAPIError(503)` 从 RETRYABLE 翻为 NON_RETRYABLE，该反例已有测试锁定。<br>**B 半场（kind → 对外业务码）未实施**：触发条件仍为空（无客户端按码分流、无按码告警或统计）。 |

2026-09-21 逐项核验候选表：C-04（无 deadline 时的流读取兜底）与 C-05（策略层整链硬超时）**确认已在代码中实现**，已移出本表并登记到[完成记录](history/completed-work.md)；C-01、C-03、C-09、C-10、C-15 确认仍未做；C-08、C-11 为部分实现（见行内）。
