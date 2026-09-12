# 部署说明文档

> **更新日期**：2026-09-12
> **文档定位**：本地开发运行、环境与基础设施验收的正式入口。
> **核验边界**：治理文档已接入完整仓库，已核对 pyproject、命令目标与对齐脚本，并运行文档对齐校验。本轮未启动服务、连接基础设施或运行产品测试；模块状态与验证范围见 [ALIGNMENT.md](../ALIGNMENT.md)。

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
```

当前 pyproject 配置 `testpaths=tests`、`asyncio_mode = "auto"`，开发依赖包含 pytest-asyncio；异步测试无需重复添加 `@pytest.mark.asyncio`。测试范围与通过条件按 [项目工作流](../engineering/project-workflow.md#verification)执行；改文档后需检查对齐脚本结果，不能把命令清单视为已通过记录。

独立脚本采用 `uv run python -m scripts.xxx`，避免直接运行 `uv run ./scripts/xxx.py` 改变导入路径。独立脚本顶部必须初始化 UTF-8，使用 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；包装流或测试替身需检查能力并提供等效编码处理，不能省略 UTF-8 输出要求。此约定只负责控制台编码，不允许把密钥写入输出。

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

## 相关文档

- [架构设计](architecture.md)
- [产品定位](product.md)
- [模块状态登记](../ALIGNMENT.md)
- [配置参考](../config_doc/config.md)
- [API 层说明](../api_doc/README.md)
- [基础设施说明](../infrastructure_doc/infrastructure.md)
- [安全说明](../platform_doc/security.md)
