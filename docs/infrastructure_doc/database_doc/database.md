# 数据库运行时（database.py）设计说明

> 对应代码：`app/infrastructure/database.py`
> 文档定位：数据库资源唯一 Owner 的内部协作契约与维护说明
> 更新日期：2026-09-22
> 状态与映射：[ALIGNMENT](../../ALIGNMENT.md) 的 `app/infrastructure/database.py` 条目；层次定位与模块地图见[基础设施说明](../infrastructure.md)
> 术语：asyncio / SQLAlchemy / PostgreSQL 名词与本项目用语见[术语表](#术语表)

## 目录

- [数据库运行时（database.py）设计说明](#数据库运行时databasepy设计说明)
  - [目录](#目录)
  - [设计目标与边界](#设计目标与边界)
  - [内部协作契约](#内部协作契约)
  - [核心概念与机制](#核心概念与机制)
    - [准入状态机](#准入状态机)
    - [原因码](#原因码)
    - [探测的只读约束与预算](#探测的只读约束与预算)
    - [池事件与连接归属](#池事件与连接归属)
  - [结构与协作](#结构与协作)
  - [状态与执行流程](#状态与执行流程)
    - [探测调度与执行](#探测调度与执行)
    - [释放流程](#释放流程)
  - [关键实现说明](#关键实现说明)
  - [行为边界](#行为边界)
  - [配置关联](#配置关联)
  - [验证入口](#验证入口)
  - [设计决策与问题记录](#设计决策与问题记录)
  - [术语表](#术语表)
    - [Python 与 asyncio](#python-与-asyncio)
    - [SQLAlchemy 与连接池](#sqlalchemy-与连接池)
    - [PostgreSQL 与驱动](#postgresql-与驱动)
    - [本项目用语](#本项目用语)
  - [相关文档](#相关文档)

---

## 设计目标与边界

`DatabaseRuntime` 是数据库资源的唯一 Owner：持有一个 engine、其连接池与 sessionmaker，向基础设施适配器提供受控 Session 工厂，并以只读探测决定能否开放新的业务准入。

本组件负责引擎与池的创建、真实连接探测、准入判定、Session 与连接的 Owner 登记、有界等待以及物理释放核验。下列责任在别处：

| 责任 | 归属 |
| --- | --- |
| 完整 schema 校验（历史、名称、校验和、结构、权限） | 唯一迁移实现，经构造参数 `schema_check` 以只读异步回调接入（DB-F03/DB-F04） |
| 迁移执行与 CLI | DB-F03 |
| 会话专有 Store、应用层 SQL 迁出 | DB-F05a |
| 应用装配、使用方 drain、readiness API | DB-F05 |
| 真实 PostgreSQL 验收 | DB-F06 |

不在应用启动时自动迁移，不自动重试业务事务，不建设后台重连或排队机制，不接管消费者排空，也不承诺强制退出进程。真实 PG 交付前，本组件的连接与事务假设只有 fake 边界和连接拒绝路径的证据。

**交接**：完整检查器尚未存在，缺失时即使 `SELECT 1` 成功也不得开放工厂；消费方先 drain 再 dispose 归 DB-F05；ADR D5 契约中的 `restart_required`（首次启动失败后要求重启）由装配期产生，本模块只做原因分类。

---

## 内部协作契约

`DatabaseRuntime` 只供基础设施适配器与其后续 Store 使用，不进入 Application 或 Domain。

| 符号 | 输入 | 输出 / 异常 | 协作方责任 |
| --- | --- | --- | --- |
| `__init__(..., timeouts, schema_check=None)` | 连接/池参数与 `DatabaseTimeouts`（见[配置关联](#配置关联)） | 构造不触发任何 I/O | 不传入 `database_config` 全量字段；数值与 URL 校验在 Settings 完成 |
| `init()` | 无 | `DatabaseStatus`；未知程序缺陷抛 `DatabaseRuntimeError("internal_error")` | 重复调用不重建引擎；closing/closed 时直接返回状态 |
| `ping()` | 无 | `bool` | 只报告本次检查结果，不改变准入，也不证明业务可写 |
| `probe()` | 无 | `DatabaseStatus`（更新后的快照） | 完整检查通过才开放工厂；显式调用是本模块唯一的准入恢复入口 |
| `session_factory` | 无 | `Callable[[], AsyncSession]`；未准入时抛 `DatabaseRuntimeError` | 每次操作取独立 Session 并负责 `close()`；缓存工厂前须知道它每次调用都会复查准入 |
| `status` | 无 | 不可变 `DatabaseStatus(state, reason, schema_version)` | 快照不含 URL、SQL 或驱动异常；`ready` 只表示检查器接受的时点观察 |
| `dispose(deadline=None)` | 可选父级单调时钟绝对期限 | `DatabaseStatus` | 先停止新准入；未完成返回 closing + `close_incomplete`，调用方可显式重试 |

`DatabaseRuntimeError` 只携带稳定原因码，不保留驱动异常文本或异常链；错误分类见[核心概念与机制](#核心概念与机制)。

---

## 核心概念与机制

### 准入状态机

| 状态 | 进入方式 | 新业务准入 | 离开方式 |
| --- | --- | --- | --- |
| `new` | 构造 | 关闭 | `init()` 建引擎并探测后转 `ready` / `unavailable` |
| `ready` | 完整探测通过 | 开放 | 探测失败转 `unavailable`；`dispose()` 转 `closing` |
| `unavailable` | 探测失败或构建失败 | 关闭 | 显式 `probe()` / `init()` 恢复为 `ready`；`dispose()` 转 `closing` |
| `closing` | `dispose()` 开始 | 关闭 | 资源全部释放转 `closed`；仍有 Owner 时保持并报 `close_incomplete` |
| `closed` | 释放完成 | 关闭 | 终态；`init()` / `dispose()` 只返回该状态 |

状态只能由本组件的探测与关闭路径写出：迟到的探测结果在写回前复查状态，关闭已开始就不再开放准入。

### 原因码

探测与关闭失败按稳定原因码分类，供 `status.reason` 与 `DatabaseRuntimeError` 共用：

| 原因码 | 来源 |
| --- | --- |
| `starting` | 初始状态；已有未完成的探测任务时拒绝重入 |
| `driver_missing` | `ModuleNotFoundError` |
| `connection_failed` | `OSError` / `ConnectionError` / SQLSTATE `08*` |
| `authentication_failed` | SQLSTATE `28000`、`28P01` |
| `permission_denied` | SQLSTATE `42501`、`25006` |
| `schema_missing` | SQLSTATE `42P01`，或检查器以该码拒绝 |
| `schema_mismatch` | SQLSTATE `42703`、版本观测值非法、检查器缺失，或检查器以该码拒绝 |
| `timeout` | 期限守卫、`TimeoutError`、池等待超时、SQLSTATE `57014` |
| `closing` / `close_incomplete` | 关闭已开始；清理失败或仍有未释放资源 |
| `internal_error` | 其余未知缺陷：不伪装成可恢复的数据库故障 |

`DatabaseRuntimeError` 只在码值落在受信集合内时保留原文：`schema_missing`、`schema_mismatch`、`permission_denied` 来自受信检查器的结论，`closing`、`close_incomplete` 由本运行时的准入与清理路径抛出；集合之外的文本一律按未知缺陷处理，不回显任意异常文本。

### 探测的只读约束与预算

一次探测是单个只读事务：`begin()` → `SET TRANSACTION READ ONLY` → `SELECT 1` → `SELECT max(version) FROM public.schema_versions` → `schema_check(connection)`，结束时不提交，由连接关闭回滚。

- 版本号只是观测值，不代替完整历史、名称、校验和、head、结构与权限核验；非法或缺失版本（含 `0`、负数、非整数）一律按 `schema_mismatch` 处理。
- 检查器须区分探针自身设置的 `transaction_read_only` 与账号默认只读/恢复模式：探针只读不能作为账号无写权限的证据。
- 预算取自配置上限：探测总期限为 `probe_timeout`，其中为清理预留 `min(cleanup_timeout, probe_timeout / 2)`；清理阶段本身受 `cleanup_timeout` 约束。
- 不做自动重连或排队：已有未完成的探测任务时，新的 `probe()` / `ping()` 直接返回 `starting` / `False`。

### 池事件与连接归属

`connect` / `checkout` / `checkin` 都是 `PoolEvents`；异步引擎经 `sync_engine` 转发池事件，监听它等价于监听本引擎自己的池。三个钩子只维护归属事实，不做重连、排队或业务重试。

| 事件 | 触发时机 | 本组件在该钩子内的动作 |
| --- | --- | --- |
| `connect` | 池新建一个驱动，早于方言初始化 SQL | 顺带回收已关闭驱动，避免 `_drivers` 随重连次数无界增长；登记 `_drivers[driver] = 当前任务`；若状态已是 `closing` / `closed`，终止该驱动并抛 `DatabaseRuntimeError("closing")` |
| `checkout` | 连接借出、业务取得连接之前 | 除“未停止的探针任务”外一律复查准入，未通过则终止该驱动并向上抛错；通过后写 `_borrowed[record]` 与 `_drivers[driver]` |
| `checkin` | 连接归还池 | 移除 `_borrowed[record]`；把 `_drivers[driver]` 置 `None` |

- `insert=True` 让 `_connect` 排在其他监听器之前：方言初始化 SQL 尚未执行、驱动已被登记，初始化失败时不会留下无主驱动。
- 准入复查的跳过条件（代码写作 `owner is not self._probe_task or self._probe_stopped`）等价于“调用方就是探针任务、且探针未被停止”：首次探测时状态尚非 `ready`，没有该豁免就永远建不了连接。`_probe_stopped` 置位后探针自己也照常复查并被拒绝，避免取消后的探针继续建立新连接。
- `checkin` 把 Owner 置 `None` 而不删除条目：`_drivers` 必须保留每个已登记驱动以核验物理关闭状态，`None` 表达“在池中空闲”，`dispose()` 据此区分可安全强制终止的空闲驱动与仍被业务持有的驱动（见[资源责任](#状态与执行流程)）。
- `pool_pre_ping` 与本组件的探针任务无关：前者由 SQLAlchemy 在借用路径内同步执行存活检查，不产生独立任务、也不需要豁免；`_probe_task` 是本组件自己的只读探测事务（见[探测的只读约束与预算](#探测的只读约束与预算)）。

---

## 结构与协作

```text
DatabaseRuntime（唯一 Owner）
├── engine + AsyncAdaptedQueuePool        ← init() 首次创建；dispose() 释放（当前默认池，代码不依赖其类名）
├── async_sessionmaker(class_=_RuntimeSession)
├── _probe_task / _dispose_task           ← 各至多一个资源工作任务
└── 三类资源登记
    ├── _sessions   : 已创建未关闭的 Session
    ├── _borrowed   : 池已借出、尚未归还的连接记录
    └── _drivers    : 物理驱动 → 当前 Owner 任务（None 表示在池中空闲）

基础设施适配器 / Store
        │  session_factory()
        ▼
_RuntimeSession（创建即登记，close 成功才解除）
        │
        ▼
连接池 → AsyncConnection（只读探测）→ 业务事务
```

- 私有 Session 子类与池事件（`connect` / `checkout` / `checkin`）共同覆盖尚未取得连接、方言初始化、`pre_ping` 以及 `AsyncSession.__aexit__` 的后台 shielded close 阶段。
- 实例日志过滤器挂在引擎实例自己的 engine logger 与池 logger 上：名字由 `logging_name` / `pool_logging_name` 生成，对象直接从实例读取，避免影响其他引擎，也不因更换池实现而漏挂（见 [DB-009](../../../issues/infrastructure/database/2026-09-23-pool-logger-name-hardcode.md)）。

---

## 状态与执行流程

### 探测调度与执行

`init()` / `probe()` / `ping()` 三个公开入口共用同一条私有链路：`_run_probe(full)` 只做调度，`_probe(full, work_deadline)` 才执行探测，执行侧不写状态。

```text
init() / probe() / ping()
  └─ _run_probe(full)                     调度：并发闸门、预算拆分、双信号等待、结果分类、复位
       ├─ 引擎缺失，或状态已是 closing / closed → 不启动工作任务
       ├─ 清理失败已记录 → 返回 close_incomplete，不重开准入
       ├─ 已有未完成探测任务 → 返回 starting（不排队、不重连）
       └─ 按预算拆分出工作期限，创建唯一探测任务
            └─ _probe(full, work_deadline)  执行：单次只读事务，逐步守卫
                 ├─ 正常完成 → 返回 (reason, version)
                 ├─ 内部缺陷 → 置 unavailable 并抛 DatabaseRuntimeError("internal_error")
                 ├─ 进入清理阶段 → 额外等待上限取 min(总期限, 清理截止时间)
                 ├─ 超时 → 取消任务并有界等待收尾
                 └─ 取消不合作 → 保留任务与资源，返回 close_incomplete
```

- **两个期限分工**：执行侧只拿到工作期限 `work_deadline`，逐步守卫用它；收尾等待用总期限，并可再与清理截止时间取小。两个数值怎么算见[探测的只读约束与预算](#探测的只读约束与预算)。
- **清理信号是提前收尾的入口**：执行侧进入 `finally` 时置位 `_cleanup_started`；调度侧与探测任务同时等待这个信号（`FIRST_COMPLETED`），清理先到时按 `min(总期限, 清理截止时间)` 追加等待，避免"探测已在收尾、调度侧还在空等"。
- **`_cleanup_failed` 是单向标记**：只在构造时置 `False`，此后任何一次清理失败都置 `True`，代码中没有复位路径；置位后 `_run_probe` 返回 `close_incomplete`、`_require_ready` 直接拒绝。它不同于可自愈的熔断，恢复只能重建运行时。同一批状态里 `_probe_stopped` 与 `_probe_failure` 相反，每次探测开始都会复位。
- **守卫只作用在语句之间**：`_guard_probe` 在发起下一个操作前检查停止标记与期限，它打断不了已经挂起的 `await`；真正中止挂起语句的是调度侧的等待超时加 `_stop_probe` 的取消，两层缺一都会留下无法收尾的探测。清理路径不调用守卫，因此收尾只受调度侧与 `dispose()` 的等待上限约束。
- **状态写回不在执行侧**：`_probe` 只返回 `(reason, version)` 并记录 `_probe_failure`；正常路径由 `probe()` 写回，`_run_probe` 只在完整探测的内部缺陷与取消路径上写回。
- **只有完整探测改写准入**：内部缺陷在 `full=True` 路径上置 `unavailable` 并抛 `DatabaseRuntimeError("internal_error")`；`ping()` 走 `full=False`，只把原因码归约为 `False`，既不写状态也不外抛（[DB-004](../../../issues/infrastructure/database/2026-09-23-ping-admission-write.md)）。

执行侧的步骤，每步之前都过一次 `_guard_probe`：建连并 `start()` → `begin()` → 声明只读 → 连通性查询 → 版本观测 → 版本合法性与 `schema_check`（后两步仅 `full=True`）。语句序列与只读保证见[探测的只读约束与预算](#探测的只读约束与预算)，清理分支（`started` 标志决定是否调用 `close()`）见[关键实现说明](#关键实现说明)。

`_stop_probe(task, deadline)` 是唯一的停止流程：显式接收本次 worker → 仅当它仍是当前探针时置 `_probe_stopped` → 未完成则取消指定任务 → 按 `min(传入期限, 现在 + cleanup_timeout)` 有界等待（仍为当前探针且清理已开始时，再与清理截止时间取小）→ `finally` 中只终止 Owner 为指定 worker 的驱动 → 任务完成则取走结果，仅在共享引用仍指向它时清空 `_probe_task`，返回收尾是否完成。

调用级取消和超时传入 `_run_probe` 捕获的局部 `task`；`dispose()` 则传入关闭开始时的当前探针。旧调用取消时，仅在它仍持有当前探针身份的情况下写回 `unavailable/starting`。worker 完成不等于外层调用已返回：后继调用可能已替换共享引用，因此取消、停止标记与物理清理均不能重新通过共享引用寻找目标。完整 A/B/W1/W2 复现图示、可执行代码和修复前后结果见 [DB-010](../../../issues/infrastructure/database/2026-09-23-stale-probe-cancellation.md)。

### 释放流程

`dispose()`：

```text
dispose(deadline=None)
  ├─ 已是 closed → 直接返回
  ├─ 置 closing 关闭新准入；期限取 min(传入值, 现在 + shutdown_timeout)
  ├─ 有界停止探测任务；仍有 Session / 借出连接 / 被持有的驱动 → 保留资源，返回 closing + close_incomplete
  ├─ 引擎不存在 → closed
  ├─ 已到期 → 不启动新的释放动作，返回 closing + close_incomplete
  ├─ 复用或新建唯一关闭任务，等待到期限
  │    ├─ 完成且 `_dispose_engine` 返回 True → closed
  │    └─ 否则 → 终止空闲驱动，返回 closing + close_incomplete
  │         （已完成但失败的清空任务引用，下次 dispose 新建；未完成则保留引用）
  └─ 调用方取消 → 置 closing + close_incomplete 后原样抛出，不吞信号
```

资源责任：

| 资源 | 创建 | 登记 | 正常释放 | 异常责任 |
| --- | --- | --- | --- | --- |
| engine / pool | `init()` | `_engine` | `dispose()` | 构建失败置 `unavailable`；未知缺陷抛 `internal_error` |
| Session | 每次 `session_factory()` 调用 | `_sessions`（创建即登记） | 调用方 `close()` 成功后才解除 | close 失败或被取消时保留 Owner，禁止重新开启 |
| 借出连接 | 池 checkout | `_borrowed` + `_drivers` | checkin | 非探测任务的 checkout 复查准入，未通过即终止该驱动并抛错 |
| 物理驱动 | `connect` 事件 | `_drivers` | 归还池后由 dispose 释放 | 关闭不能完成时另行 terminate，且仅限探针自有驱动或已无业务 Owner 的空闲驱动；失败记 `close_incomplete`，不伪报 `closed` |
| 探测任务 | `init()` / `ping()` / `probe()` | `_probe_task` | 任务自身完成，或由关闭路径有界停止 | 取消不合作时保留任务与资源引用 |
| 关闭任务 | `dispose()` | `_dispose_task` | 任务自身完成，或再次 dispose 复用同一任务 | 未完成（超时/取消）保留引用，下次 dispose 复用；已完成但失败则清空引用，供下次新建重试 |

---

## 关键实现说明

以下为维护者必须知道的非显然约束，实现见 `app/infrastructure/database.py` 对应符号。

- **显式 `start()` / `close()`**：探测不使用 `AsyncConnection.__aexit__`，因为它会创建 shield 的后台 close 任务，把连接 Owner 移交出当前作用域。
- **未启动即不关闭**：包装对象存在不等于已取得连接；`start()` 失败路径只终止已登记的驱动，不对未启动的连接调用 `close()`（见 [DB-002](../../../issues/infrastructure/database/2026-09-22-unstarted-connection-cleanup.md)）。
- **构建完成才发布引擎**：`_build_engine` 在局部变量里构造引擎、监听器与工厂，全部就绪后才连续赋值发布，因此「`_engine` 非空」等价于「构建完成」；构建中途失败只置 `unavailable`，不留下半个引擎，下一次显式 `init()` 从零重建（见 [DB-007](../../../issues/infrastructure/database/2026-09-23-half-built-engine-admission.md)）。
- **只终止自己有权处理的驱动**：`_terminate_probe_drivers(task)` 只覆盖 Owner 为指定 worker 的驱动，参数为 `None` 时不处理任何驱动；worker 自身清理传入 `asyncio.current_task()`，停止流程传入其局部任务。`_terminate_idle_drivers` 只覆盖 Owner 为 `None` 的空闲驱动——两处都在代码内限定范围，不依赖调用方守纪律；业务借出的连接由使用方负责，`dispose()` 在仍有 Owner 时保留资源并报告未完成（见 [DB-003](../../../issues/infrastructure/database/2026-09-22-runtime-resource-ownership.md)、[DB-006](../../../issues/infrastructure/database/2026-09-23-idle-driver-termination-guard.md)、[DB-010](../../../issues/infrastructure/database/2026-09-23-stale-probe-cancellation.md)）。
- **释放需要物理核验**：`engine.dispose()` 正常返回不作为释放成功证据，另核验驱动 `is_closed()`，因为池可能吞掉关闭异常并自行写日志；该调用只关闭池中连接，借出的连接不在其范围内，所以必须先通过 Owner 守卫。
- **日志与凭证**：实例日志过滤器把池与 echo 事件压为固定文本和原级别，连同 `hide_parameters` 保证不输出 SQL、参数、凭证或异常链。
- **工厂每次复查准入**：`session_factory` 属性访问与生成的工厂调用都检查准入，避免缓存工厂在关闭后继续放行。
- **关闭后不可复用**：sessionmaker 使用 `close_resets_only=False`，关闭后的 Session 再次 `begin()` 会抛 `InvalidRequestError`，防止已解除 Owner 的 Session 被当空对象复用。

---

## 行为边界

| 场景 | 行为 | 约束或验证入口 |
| --- | --- | --- |
| 探测期间再次探测 | 直接返回 `starting` / `False`，不排队、不重连 | 并发探针只建立一个连接任务 |
| 取消当前 `init()` / `probe()` | 有界清理后向上抛 `CancelledError`，关闭本次探测准入；后继探测状态不由旧取消覆盖 | 无后继探测时状态不再是 `ready` |
| 旧 worker 完成后，旧调用取消与后继探测交错 | 旧调用只清理自己的 worker，取消继续上抛；后继探测可正常完成 | 新 worker 不被误取消，新借出驱动不被旧清理终止 |
| 取消或重启 `dispose()` | 保留关闭任务引用，返回 `closing` + `close_incomplete` | 显式再次 dispose 可继续收尾 |
| 探测任务吞取消 | 保留任务与资源，报告 `close_incomplete`，不把 timeout 当物理释放 | 迟到的探测结果不得重新开放准入 |
| 仍有业务 Owner 时 dispose | 不 dispose engine、不强制关闭连接 | 使用方排空后再次 dispose 才可能到 `closed` |
| 关闭期新建连接 | 终止该驱动并抛 `DatabaseRuntimeError("closing")`，不留可用驱动 | 暂无直接断言，仅由构造与关闭路径约束 |
| Session 关闭失败或被取消 | 保留 Owner，`dispose()` 报 `close_incomplete` | 不伪报 `closed` |
| 父级期限已过期 | 不启动新的释放动作，直接报 `close_incomplete` | 传入的绝对期限与自身预算取小 |
| 重复 `dispose()` | 幂等：已 `closed` 直接返回，未完成可重试 | 不产生第二次释放副作用 |
| 关闭失败后的清理 | 清理失败不覆盖先发生的主原因码 | 同时阻止后续准入 |

---

## 配置关联

构造参数全部来自 Settings 已校验的字段，其中六项超时经不可变 `DatabaseTimeouts` 聚合传入；键、类型、默认值与约束只在[配置参考](../../config_doc/config.md#7-数据库配置)维护。映射关系：

六项秒数由同文件的 `DatabaseTimeouts` 聚合，使用 `@dataclass(frozen=True, kw_only=True)`，作为 `DatabaseRuntime(timeouts=...)` 的必填参数。运行时持有同一不可变对象，不另存探测、清理和关闭秒数副本。该对象只表达配置数据，不设默认值或重复校验；装配期从 Settings 显式取值构造，不让 Settings 依赖基础设施类型。

| 运行参数 | 配置键 | 作用 |
| --- | --- | --- |
| `url` / `pool_size` / `max_overflow` / `echo` | `DATABASE_URL` / `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` / `DATABASE_ECHO` | 引擎与池容量；echo 经实例过滤器脱敏 |
| `timeouts.connect_timeout_seconds` / `timeouts.operation_timeout_seconds` | `DATABASE_CONNECT_TIMEOUT_SECONDS` / `DATABASE_OPERATION_TIMEOUT_SECONDS` | 驱动连接等待与单命令上限（`command_timeout`） |
| `timeouts.pool_timeout_seconds` | `DATABASE_POOL_TIMEOUT_SECONDS` | 从池获取连接的等待上限 |
| `timeouts.probe_timeout_seconds` / `timeouts.cleanup_timeout_seconds` / `timeouts.shutdown_timeout_seconds` | 同名 `DATABASE_*_TIMEOUT_SECONDS` | 探测总预算、清理预留与关闭总预算 |

`pool_pre_ping` 与 `hide_parameters` 由本组件固定开启，不设配置键；`pre_ping` 只检查取出连接的存活，不挽救中断的事务，也不替代业务重试契约。

`Settings.database_config` 同时包含迁移两项预算与连接凭证，**不能整体展开为构造参数或写入日志**；装配期须按键选择（归 DB-F05）。

---

## 验证入口

| 测试文件 | 覆盖的可观察行为 |
| --- | --- |
| `tests/unit/test_database.py` | 准入前后差异、只读语句序列与版本观测、原因码分类、运行期稳定码按原码上报、未受信文本归未知缺陷、工厂保护、探测超时、取消传播、不合作任务、并发探测与并发 dispose、重复关闭、父级期限、外部 Owner 不强关、关闭失败保留主原因、内部缺陷下 ping 不改准入、半构建引擎不开放准入、强制终止只处理空闲驱动、关闭期连接被终止并以 `closing` 拒绝、脱敏目标跟随引擎实例、真实引擎连接拒绝、真实引擎 echo 与池日志脱敏、驱动可导入 |
| `tests/unit/test_database_lifecycle_review.py` | 真实 SQLAlchemy Session 的 Owner 责任：close 被取消、上下文 shielded close 未完成、关闭后禁止复用 |
| `tests/unit/test_database_settings.py` | 配置注入与校验（属配置契约，见[配置参考](../../config_doc/config.md#7-数据库配置)） |

测试用 fake 只证明控制流；真实连接、schema 与 CRUD 的成功路径尚无证据，见 [DB-F06](../../todo.md#db-foundation)。当前实现与接线状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准，运行结果与评审记录见 [DB-F02 评审](../../todo.md#db-f02-review)。

---

## 设计决策与问题记录

| 记录 | 关联内容 |
| --- | --- |
| [DB-ADR-001 D4/D5](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d4运行时与故障语义) | 运行时接口、故障语义与公共 API 契约的决策正文 |
| [DB-ADR-001 D6](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d6生命周期接线) | 生命周期接线与关闭顺序 |
| [DB-001](../../../issues/infrastructure/database/2026-09-22-missing-asyncpg-dependency.md) | 驱动依赖缺失的闭环 |
| [DB-002](../../../issues/infrastructure/database/2026-09-22-unstarted-connection-cleanup.md) | 未启动连接的清理误判 |
| [DB-003](../../../issues/infrastructure/database/2026-09-22-runtime-resource-ownership.md) | Session/连接 Owner 遗漏与并发任务引用竞争 |
| [DB-004](../../../issues/infrastructure/database/2026-09-23-ping-admission-write.md) | `ping()` 观测在内部缺陷时写入准入状态 |
| [DB-005](../../../issues/infrastructure/database/2026-09-23-runtime-reason-classification.md) | 运行期自身原因码被归类为未知缺陷 |
| [DB-006](../../../issues/infrastructure/database/2026-09-23-idle-driver-termination-guard.md) | 空闲驱动的强制终止缺少 Owner 过滤 |
| [DB-007](../../../issues/infrastructure/database/2026-09-23-half-built-engine-admission.md) | 半构建引擎会开放准入且丢失跟踪与脱敏 |
| [DB-008](../../../issues/infrastructure/database/2026-09-23-connect-during-close-assertion.md) | 关闭期连接拦截缺少直接断言 |
| [DB-009](../../../issues/infrastructure/database/2026-09-23-pool-logger-name-hardcode.md) | 池实例 logger 名硬编码，换池实现会静默失效 |
| [DB-010](../../../issues/infrastructure/database/2026-09-23-stale-probe-cancellation.md) | 旧探测取消误伤后继任务：完整时序图、代码与回归证据 |

---

## 术语表

本表解释本文件用到的外部库概念与项目用语，供不熟悉 asyncio / SQLAlchemy 的读者查用。正文已完整定义过的机制（准入状态机、原因码、资源责任）只给指针，不在此重复展开。

### Python 与 asyncio

| 术语 | 说明 |
| --- | --- |
| 协程与任务（coroutine / Task） | `async def` 定义的是协程，本身不会执行；被事件循环安排执行时才成为 Task。本组件的归属记录记的就是“哪个 Task 正在用这条连接”。 |
| 取消（`CancelledError`） | 向任务投递的“请收尾退出”信号，不是普通故障。任务可以先清理再退出，也可以不合作；不合作的取消会让关闭无法在期限内完成。 |
| 事件循环（event loop） | 调度协程的执行器。本组件全程用 `asyncio.get_running_loop().time()` 取时间，不读系统时钟。 |
| 单调时钟与绝对期限（deadline） | 单调时钟只前进不回拨，适合度量时间跨度。`deadline` 是一个绝对时刻；各阶段用它减去当前时刻得出自己还剩多少预算，避免层层等待把总时长叠加上去。 |
| `shield` / 受保护的关闭 | 被 `asyncio.shield` 保护的任务，在调用方被取消时仍会继续执行。正因为它会把关闭动作移出当前作用域，探测不使用 `AsyncConnection.__aexit__`（见[关键实现说明](#关键实现说明)）。 |
| `await` 与 I/O 边界 | `await` 是任务唯一可能被切换或取消的位置；判断取消后是否还会继续发请求，就是看 `await` 之间做了什么。 |
| `asyncio.wait` 与 `FIRST_COMPLETED` | 同时等多个任务，`FIRST_COMPLETED` 表示任意一个先完成就返回，而不是等全部结束。调度侧用它同时等“探测做完”和“清理开始”两件事。 |

### SQLAlchemy 与连接池

| 术语 | 说明 |
| --- | --- |
| 引擎（`Engine` / `AsyncEngine`） | SQLAlchemy 对“数据库 + 连接池”的整体封装。异步引擎内部包着一个同步引擎，池与底层连接由后者管理，所以池事件挂在 `sync_engine` 上。 |
| 方言（dialect） | 针对某种数据库与驱动的适配层，负责生成该库的 SQL、类型与连接初始化动作；本项目是 PostgreSQL + asyncpg 方言。 |
| 连接池与容量 | 复用物理连接的容器。`pool_size` 是常驻连接数，`max_overflow` 是允许临时超出的数量，`pool_timeout` 是池中无空闲连接时的等待上限。 |
| 池事件（`PoolEvents`） | 池生命周期上的钩子。本组件只用 `connect`（连接创建）、`checkout`（借出）、`checkin`（归还）三点做归属登记，见[池事件与连接归属](#池事件与连接归属)。 |
| Session 与 `async_sessionmaker` | Session 是一次事务边界与工作单元对象；`async_sessionmaker` 是生产 Session 的工厂。业务只能通过本组件的工厂取得 Session，才能被登记 Owner。 |
| `AsyncConnection` 包装对象 | `engine.connect()` 返回的异步连接包装器。“对象已创建”不等于“已连上数据库”，因此 `start()` 失败的路径不能对它调 `close()`。 |
| `pre_ping`（`pool_pre_ping`） | 借出连接前先做一次轻量检查，发现连接已死就换一条。它只证明连接活着，不挽救中断的事务，也不替代业务重试。 |
| `echo` 与 `hide_parameters` | `echo` 让 SQLAlchemy 把执行语句写进日志，`hide_parameters` 让日志中的参数变成占位符。两者都不足以挡住凭证，所以本组件另加实例级日志过滤器。 |
| `expire_on_commit` | 置 `False` 时 commit 后对象属性仍然有效；否则下次读取已过期属性会触发一条新查询。本组件不希望关闭与取消阶段冒出意外查询。 |
| `close_resets_only` | 置 `False` 时 Session 一旦 close 就不可复用；理由见[关键实现说明](#关键实现说明)。 |
| logger 与 Filter | Python 标准日志设施。Filter 是每条日志写出前必经的钩子，本组件用它把内容替换为固定文本。 |

### PostgreSQL 与驱动

| 术语 | 说明 |
| --- | --- |
| DBAPI 驱动与 asyncpg | 真正与数据库通信的库；DBAPI 是这套接口的标准，本项目唯一后端是 asyncpg。驱动缺失会被单独分类，见[原因码](#原因码)。 |
| `timeout` / `command_timeout` | asyncpg 的连接建立等待上限与单条命令执行上限，分别取自 `connect_timeout_seconds` 与 `operation_timeout_seconds`。 |
| 只读事务 | 数据库层面禁止写入的事务。探测用 `SET TRANSACTION READ ONLY` 声明，结束时由连接关闭回滚，保证探测不改数据。 |
| SQLSTATE | PostgreSQL 的五位标准错误码，前两位表示类别（如 `08` 为连接类异常）。原因码分类就是按它映射的，完整对照见[原因码](#原因码)。 |
| schema 与 `schema_versions` | schema 指数据库结构（表、列、约束）的统称，也指 `public` 这类命名空间。`public.schema_versions` 是本项目记录已应用迁移版本的唯一表。 |
| 迁移 / 校验和 / 基线 | 迁移是版本化演进表结构的 SQL 序列；校验和用哈希比对迁移文件是否被改动；基线是给存量表登记起始版本，避免重放历史迁移。 |

### 本项目用语

| 术语 | 说明 |
| --- | --- |
| 准入（admission）与 `ready` | 是否允许业务取到新 Session 的判定，各状态含义见[准入状态机](#准入状态机)。`ready` 只是某个时点的观察，不代表数据库此后一直可用。 |
| 探测（probe） | 本组件自己发起的只读检查，也是唯一的准入恢复入口。语句序列与预算见[探测的只读约束与预算](#探测的只读约束与预算)。 |
| fail closed | 检查不完整或无法确认时按“不可用”处理，而不是默认放行。schema 检查器缺失时即使 `SELECT 1` 成功也不开放工厂，就是这个原则。 |
| 资源 Owner | 对某个资源负责创建与释放的唯一责任方。本组件登记 Session、已借出连接、物理驱动三类，见[资源责任](#状态与执行流程)。 |
| 有界等待 | 等待带明确上限，到点就返回当前结论，不无限等。关闭路径上的每一步等待都是有界的，未完成时报告 `close_incomplete`。 |
| 幂等 | 重复执行与执行一次结果相同。公开契约要求重复 `dispose()` 不产生第二次释放副作用。 |
| `DB-F01`…`DB-F06` | 本次数据库基础设施的分片编号，进度与验收见 [DB-F 计划](../../todo.md#db-foundation)。 |
| `D4` / `D5` / `D6` | [DB-ADR-001](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md) 内的决策编号，分别对应运行时与故障语义、公共 API 与能力契约、生命周期接线。 |
| ALIGNMENT | 实现、接线、文档与测试状态的唯一登记表，见[代码—文档—测试对齐](../../ALIGNMENT.md)。 |

---

## 相关文档

- [基础设施说明](../infrastructure.md) — 本层定位、模块地图与容器直管现状
- [配置参考](../../config_doc/config.md#7-数据库配置) — 数据库配置键、默认值与校验约束
- [部署与验证](../../project/deployment.md) — 运行命令与验证入口
- [DB-F 计划](../../todo.md#db-foundation) — 分片进度、评审与后续交接
- [代码—文档—测试对齐](../../ALIGNMENT.md) — 实现、接线与测试状态
