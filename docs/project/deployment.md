# 部署说明文档

> **更新日期**：2026-09-21
> **文档定位**：本地开发运行、环境与基础设施验收的正式入口。
> **核验边界**：治理文档已接入完整仓库，已核对 pyproject、命令目标与对齐脚本，并运行文档对齐校验。2026-09-17 补充 Ruff 命令登记，核对了 `pyproject.toml` 的 Ruff 配置与 `ruff --version` 的实测规则集；2026-09-21 建立 `ruff check .` 零错误基线并登记为提交前关口（本轮核对命令与配置，未登记长期通过状态）。本轮未启动服务、未连接基础设施，也未在本文档登记任何检查结论；模块状态与验证范围见 [ALIGNMENT.md](../ALIGNMENT.md)。

## 环境要求

| 项 | 项目文档声明与接入核验 |
| --- | --- |
| Python | 文档声明 ≥3.14；检查实际 requires-python、锁文件和运行解释器 |
| 包管理 | uv；依赖以真实 pyproject 与锁文件为准 |
| 平台 | 开发以 Windows 为主；跨平台运行需验证编码、路径、子进程与信号行为 |
| 数据库 | 会话/消息持久化依赖可用数据库与匹配异步驱动；进程能启动不等于会话链可用 |
| Redis | 用于热缓存；模块文档声明 None 时跳过缓存直查 DB，需验证该降级和 DB 可用性 |

安装依赖前检查环境与锁文件；若任务已授权安装所需依赖，可在项目根执行：

```bash
uv sync
```

## 常用命令

以下命令在仓库根运行；对应脚本与测试路径已核对存在。本轮只运行文档对齐命令，其余按实际任务选择。

```bash
uv sync
uv run python -m app.main
uv run pytest
uv run pytest tests/unit/test_retry.py
uv run python -m scripts.test_search_tool
uv run python -m scripts.verify_alignment
uv run python -m scripts.observe_reflection_deadline
uv run ruff format --check .
uv run ruff check .
```

排查时序敏感的时限用例时用 `uv run python -m scripts.observe_reflection_deadline [预算...]`：按给定总预算跑一轮 Reflection，打印每一跳耗时（重点是「外部插件刷新 + 准入」与「工具本体」的拆分）与终止形态，用于评估某个预算还剩多少余量、以及预算不足时链路先在哪一跳越界。用法与定位背景见脚本 docstring。

当前 pyproject 配置 `testpaths=tests`、`asyncio_mode = "auto"`，开发依赖包含 pytest-asyncio；异步测试无需重复添加 `@pytest.mark.asyncio`。测试范围与通过条件按 [项目工作流](../engineering/project-workflow.md#verification)执行；改文档后需检查对齐脚本结果，不能把命令清单视为已通过记录。

Ruff 配置在 `[tool.ruff]`，`line-length` 取编码规范上限 120。**E501 不在 Ruff 默认规则集内，必须由 `extend-select` 显式启用**；`[tool.ruff.format]` 排除了声明式数据表与 Markdown。`uv run ruff format --check .` 与 `uv run ruff check .` 同为提交前必经关口：前者格式化不改变 AST，但会重排换行，因此不要与语义改动混在同一提交单元；后者的零错误基线已于 2026-09-21 建立（见[完成记录](../history/completed-work.md)），新增发现须在提交前清零，不为历史代码保留例外。检查结论按实际运行报告，不在此登记通过状态。

独立脚本采用 `uv run python -m scripts.xxx`，避免直接运行 `uv run ./scripts/xxx.py` 改变导入路径。独立脚本顶部必须初始化 UTF-8，使用 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；包装流或测试替身需检查能力并提供等效编码处理，不能省略 UTF-8 输出要求。此约定只负责控制台编码，不允许把密钥写入输出。

### 行尾约定（LF）

仓库以 `.gitattributes` 声明 `* text=auto eol=lf`：检出为 LF，不再经过各机器的 `core.autocrlf`（Git for Windows 默认为 `true`，会把工作区检出成 CRLF）。

未声明时工作区会同时存在 CRLF、LF 和混行尾文件，而**这类问题在 git 里看不见**：git 按归一化后的内容比较，混行尾文件的 `git status`、`git diff`、`git add` 结果与干净版本没有差别，只有 `uv run ruff format --check .` 读工作区字节时才会失败（`[tool.ruff.format]` 的 `line-ending` 取默认 `auto`，按文件主导行尾判定）。因此新增或改写文件时保持 LF，不要使用会把行尾写成 CRLF 的写入路径。

核对工作区是否残留非 LF 行尾：

```bash
git ls-files --eol | grep -E 'w/(crlf|mixed)'   # 应无输出
```

## 启动方式

从真实仓库根以模块形式运行，避免直接脚本执行改变导入搜索路径：

```bash
uv run python -m app.main
```

若项目实际暴露 `app.main:app`，本地调试也可使用：

```bash
uv run uvicorn app.main:app --reload
```

绑定地址、端口、reload 与生产启动参数由真实入口及部署配置决定，不把示例端口硬编码成生产约束。reload 用于本地开发。

### 启动与健康检查

日志应能区分资源就绪、缓存降级和必要依赖失败；具体日志格式见 [日志说明](../platform_doc/observability/logging.md)。工具注册清单与版本值以当前实现为准。

```bash
curl http://localhost:8000/api/health
```

`/api/health` 是项目文档记录的健康端点；响应成功只能证明该端点可用。部署验收还需真实完成创建会话、保存/读取消息、一次受控 Agent 请求和资源关闭，不能仅凭 `status=ok` 宣称 DB、Redis、外部模型均可用。

## 依赖基础设施

### PostgreSQL 与异步驱动

- 用途：SessionModel/MessageModel 持久化。核验连接字符串、所需驱动、建表/迁移、读写权限及连接关闭。
- 使用 `postgresql+asyncpg` 时，检查实际环境和依赖是否包含 asyncpg；资料曾报告驱动缺失，但本轮不能断言现仓仍缺失，也不能不经检查重复增加依赖。
- 驱动导入成功、引擎对象构造成功和数据库实际连通是不同检查点。应通过真实连接与最小事务确认持久化能力。
- 若初始化允许置空降级，依赖该资源的会话接口必须有明确不可用语义；禁止以“整体启动成功”掩盖运行时失败。没有已确认的替代存储契约时，不自动增加 SQLite 等兼容后端。

### Redis 与缓存降级

- 用途：会话元数据、列表和统计缓存。键、TTL 与业务失效机制见 [会话文档](../application_doc/session_doc/session.md)，本文件不复制配置表。
- 当前会话文档声明 `_cache_get/_cache_set/_cache_delete` 对 `redis=None` 跳过缓存，转数据库路径。回仓验证缓存缺失时的创建、读取、列表及删除；不要继续沿用“Redis 缺失必然 AttributeError”的旧断言。
- 初始化时没有客户端与运行中断连/操作超时是不同失败模式，分别验证是否按契约降级；DB 同时不可用时不能承诺完整服务可用。

## 环境变量配置

配置字段、默认值与消费位置由 [配置参考](../config_doc/config.md) 和真实 `app/config/settings.py` 维护。本文件只给连接示例，不复制模型目录或配置全表。

```bash
LLM_API_KEY="<实际密钥，通过环境安全注入>"
LLM_BASE_URL="<供应商兼容 API 端点>"
LLM_MODEL_ID="<账户实际可用模型>"
DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db"
REDIS_URL="redis://localhost:6379/0"
```

示例不是可直接提交的生产配置。密钥不得硬编码、进入版本库或诊断输出。修改模型 ID 时核验对应模型窗口及配额，fallback 也检查实际目标配置；同端点不代表同窗口或共享额度。

项目声明的加载优先级：

```text
系统环境变量 > .env 文件 > 配置默认值
```

Pydantic Settings 读取 `.env` 不等于写入进程 `os.environ`，业务模块按装配注入消费配置；日志 bootstrap 的特定读取边界见 [日志说明](../platform_doc/observability/logging.md) 和 [架构说明](architecture.md#装配根)。密钥、端口及可配置运行参数不得在业务代码硬编码，使用已有配置契约与环境注入。

## 常见问题

### 直接执行 main.py 出现导入错误

先确认工作目录和入口，采用 `uv run python -m app.main`。不要依赖文档中曾记载的 sys.path 补丁一定仍存在，也不为运行示例新增导入兼容代码。

### Windows 控制台出现 UnicodeEncodeError

核验实际 stdout/stderr 编码和流能力；日志前缀、输出编码及脚本初始化须与真实环境匹配。包装流、无控制台或测试替身可能没有 reconfigure 能力，不直接套用无条件调用。

### 进程启动成功但会话接口失败

沿真实 DB 连接、事务、缓存操作和错误翻译路径定位，区分驱动缺失、服务不可达、权限失败与 Optional 客户端误用。先形成复现测试，再修对应责任层；不凭旧缺陷描述提前重写存储体系。

### 本地可运行是否等于可对外部署

不等于。核验实际认证、会话归属检查、CORS、密钥与工具执行权限。mock 鉴权、默认审批放行或命令黑名单不能被当成生产安全保障；能力是否就绪见模块状态和相关安全契约。

<a id="tool-lifecycle-p0"></a>

## 工具生命周期 P0 部署规格（待实施）

2026-09-13 规格，由 [TOOLS-ADR-008](../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)约束。本节不是已部署能力，以下脚本目标需在交付 B 实现后才能运行；当前本地 reload 入口不提供受支持的副作用工具恢复/退出保证。

### A 与 B 启用边界

A 允许同一活动进程内多 Agent、多批次共享 ToolService，以普通只读能力验证进程内取消、事实和有界接管，不宣称崩溃恢复。B 的副作用/未知/强制审计工具在驱动、schema、单机 Owner、未决保护恢复完成前不开放。注册工具可存在但不得在 schema 导出时误报为可执行；Gateway 还需最终检查，不能只靠模型可见清单。

2026-09-22 再次核实 `pyproject.toml` / `uv.lock` 未声明 asyncpg，当前环境 `find_spec("asyncpg")` 为 `None`，按默认 URL 构造 engine 实测抛出 `ModuleNotFoundError`；`scripts/init_db.py`、`scripts/migrate.py` 仍为空。本轮未安装驱动或连接 DB。共享 DB-F 实施时在依赖及锁文件中接入 asyncpg；后续 B 复用数据库运行时的 SQLAlchemy engine/sessionmaker 和 ORM Base，不因工厂构造成功就跳过真实连接、最小事务、schema 与权限检查。

### 工具 schema 迁移

> 2026-09-22：[共享数据库 ADR](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#database-supersession) 已批准，替代原工具专属目录及 `--tools` 入口。以下为已批准但尚未实现的部署规格，不能作为当前可执行步骤。

采用共享版本化 SQL 序列，首迁移为 `migrations/0001_sessions_and_messages.sql`；工具账本由 Piece⑥增加后续全局版本，`scripts/migrate.py` 实现有序升级、校验和及事务回滚；`scripts/init_db.py` 只委托同一迁移入口，不建立第二份 create_all 逻辑。使用现有连接配置，脚本不打印连接凭证。

目标命令（尚未实现）：`uv run python -m scripts.migrate`；已有兼容表需显式使用 `--baseline-existing`，严格核验后才登记基线；应用 startup 只检查版本/读写能力，不自动改 schema。每个迁移先验证 PostgreSQL 支持事务的语句；失败回滚并阻止 B，不能部分升级后宣称就绪。降级通过旧代码兼容性检查及备份恢复单独执行，不自动 DROP 未决账本。测试包括干净库升级、重复执行、校验和不符、事务中断与存量未决记录。

### 单主机单活动执行进程

首期受支持平台为 Windows，使用 `msvcrt.locking(fd, LK_NBLCK, 1)` 对固定本地锁文件首字节非阻塞独占；同一 scope 的所有启动入口由 Container 取得，同进程保持非继承句柄至受保护工作结束或进程退出。失败明确拒绝 B；不睡眠重试抢锁，不删除/重建锁文件。路径必须固定、可信且位于工具可修改范围外。参见 [Python msvcrt](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)。Linux/共享文件系统不在首期验证保证中，未来按平台补实现与验收，不静默绕过。

DB advisory lock 不能替代该存活排他：数据库会话结束会释放锁，但旧程序仍可能执行文件或远端写入，见 [PostgreSQL 锁语义](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)。OS 锁也不能阻止未遵守入口的外部进程、其他主机或任意代码故意破坏控制文件；当前为可信单主机部署契约，不是沙箱安全边界。

启动顺序：获取 scope 的 OS 锁→连接/版本检查→分页读取未决记录、恢复必要冲突限制→开放相关准入。不重放旧意图；不能仅因旧主进程退出就消除孤儿进程/远端未知写保护。数据库不可用时，只开放能证明不绕过未决保护的独立 A 能力，相关文件读取也须受保护。

### 受支持宿主与有界退出

目标入口 `uv run python -m scripts.run_tool_host --host <地址> --port <端口>`（B 新增）：父宿主只做看门狗，spawn 一个执行 worker，worker 内以 Uvicorn 单 worker、无 reload 启动应用。父进程不执行 Agent，不违背“一个活动执行进程”。当前 settings 没有应用监听 host/port，新宿主明确要求 CLI 显式提供两者（端口 1～65535），没有隐式默认值，也不复用 metrics_port；配置参考登记该边界，不硬编码新端口。

关闭请求由宿主记录总期限，经专用单向控制管道通知 worker；worker 设置 Uvicorn should_exit 并停止工具新准入。工具内部窗口内先清理/刷写；仍有真实 Owner 时报告关闭未完成并保持所需客户端/DB，不能继续假定正常 dispose。宿主总窗口的最后 20% 留给强退确认（工具正常窗口必须小于宿主窗口的 80%）；前段耗尽仍未退出时 terminate worker，join 仅用总窗口剩余时间。最终仍未确认则报告退出失败、不启动替代 worker；不能无限 join 或由解释器退出隐式等待掩盖失败。该故障下不承诺宿主已经物理退出，运维按非正常关闭处理。

强退不会运行所有 finally，也不保证后代进程终止；专用管道不用于传递执行账本，避免把可能损坏的业务队列作为恢复来源。旧 worker 未确认退出时不启动新 worker，OS 锁作为第二道检查。详见 [Python 进程终止限制](https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Process.terminate)。宿主意外崩溃、孤儿子进程和远端请求仍列未决恢复，不能宣称完全有界的分布式终止。

关闭测试必须使用可控 worker/线程与真实子进程：正常退出、清理挂起、重复关闭、双启动、DB 断连、强退重启、旧执行仍持锁；验证依赖关闭顺序及未决保护，不仅 mock shutdown 返回。首期不新增系统服务安装或自动部署，用户现有 reload 开发方式保持；需要 B 保证时使用完成验收后的受支持宿主入口。

## 相关文档

- [架构设计](architecture.md)
- [产品定位](product.md)
- [模块状态登记](../ALIGNMENT.md)
- [配置参考](../config_doc/config.md)
- [API 层说明](../api_doc/README.md)
- [基础设施说明](../infrastructure_doc/infrastructure.md)
- [安全说明](../platform_doc/security.md)
