# 基础设施层说明文档

> 数据库规划已按用户确认的文档对照结论收敛；当前实现状态见 [ALIGNMENT](../ALIGNMENT.md)。设计取舍及确认范围见 [DB-ADR-001](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#infrastructure-alignment)，不表示代码已经实施。

## 目录

- [基础设施层说明文档](#基础设施层说明文档)
  - [目录](#目录)
  - [模块概述](#模块概述)
    - [核心定位](#核心定位)
    - [模块结构](#模块结构)
  - [模块实现状态表](#模块实现状态表)
  - [现状说明](#现状说明)
    - [DB / Redis 由 container 直接管理](#db--redis-由-container-直接管理)
    - [降级策略](#降级策略)
    - [asyncpg 驱动未安装 → DB 恒降级](#asyncpg-驱动未安装--db-恒降级)
  - [规划说明](#规划说明)
    - [database.py](#databasepy)
    - [redis\_client.py](#redis_clientpy)
    - [message\_queue/](#message_queue)
  - [相关文档链接](#相关文档链接)

---

## 模块概述

基础设施层（`app/infrastructure/`）是系统的**底层资源抽象层**，负责对数据库、缓存、消息队列等外部基础设施进行统一封装，向上层服务提供稳定的访问接口。

### 核心定位

- **抽象封装**：屏蔽具体技术细节（驱动、连接池、协议）；业务用例依赖 Domain 定义的 Store Port，Infrastructure 提供适配器，Container 负责装配注入
- **生命周期管理**：统一负责资源的创建、初始化、健康检查与释放
- **可替换性**：通过接口隔离实现可替换（如缓存后端在 Redis / Memcached 之间切换）
- **解耦**：让服务层不再直接持有具体客户端对象

### 模块结构

```text
app/infrastructure/
├── __init__.py             ← 包入口，规划导出统一封装接口
├── database.py             ← 数据库封装（规划：engine / session factory）
├── redis_client.py         ← Redis 封装（规划：连接池 / 编解码 / 重连）
├── message_queue/          ← 消息队列子包
│   └── __init__.py         ← 子包入口
└── models/                 ← ORM 数据模型（已实现，见 model_doc/model.md）
    └── database/
        ├── base.py         ← Base（共享 declarative_base 实例）
        ├── session.py      ← SessionModel（会话表）
        ├── messages.py     ← MessageModel（消息表）
        ├── task.py         ← ⏳ 预留：任务表
        └── tool_log.py     ← ⏳ 预留：工具调用日志表
```

> **当前状态**：`models/database/` 的 ORM 模型（SessionModel / MessageModel）已实现并被 `SessionManager` 使用；`database.py` / `redis_client.py` / `message_queue/` 为空占位。DB / Redis 连接实际由 `app/container.py` 直接管理（见 [现状说明](#现状说明)）。

---

## 模块实现状态表

| 文件 | 状态 | 定位 |
| --- | --- | --- |
| `app/infrastructure/__init__.py` | 空（0 行） | 基础设施层包入口，规划统一导出封装接口 |
| `app/infrastructure/database.py` | 空（0 行） | 数据库引擎与会话封装（engine / session factory / 生命周期 / 健康检查） |
| `app/infrastructure/redis_client.py` | 空（0 行） | Redis 客户端封装（连接池 / 编解码 / 超时 / 重连 / 命名空间） |
| `app/infrastructure/message_queue/__init__.py` | 空（0 行） | 消息队列子包入口，规划抽象统一消息发布 / 消费接口 |
| `app/infrastructure/models/database/base.py` | [见对齐表](../ALIGNMENT.md) | 共享 `Base`（唯一 declarative_base 实例），见 [model.md](model_doc/model.md) |
| `app/infrastructure/models/database/session.py` | [见对齐表](../ALIGNMENT.md) | `SessionModel` 会话表，见 [model.md](model_doc/model.md) |
| `app/infrastructure/models/database/messages.py` | [见对齐表](../ALIGNMENT.md) | `MessageModel` 消息表，见 [model.md](model_doc/model.md) |
| `app/infrastructure/models/database/task.py` | 空（0 行） | 任务表预留，见 [model.md](model_doc/model.md) |
| `app/infrastructure/models/database/tool_log.py` | 空（0 行） | 工具调用日志表预留，见 [model.md](model_doc/model.md) |

---

## 现状说明

### DB / Redis 由 container 直接管理

当前基础设施资源**未经过** `infrastructure/` 层封装，而是由 `app/container.py` 的 `Container.initialize()` 直接创建：

**数据库**（`container.py` L103-119）：

```python
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
)
self._engine = engine  # 显式持有引用，shutdown 时 dispose()
self.db_session_factory = async_sessionmaker(engine, expire_on_commit=False)
```

**Redis**（`container.py` L88-99）：

```python
self.redis = Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)
await self.redis.ping()
```

**调用链现状**：`Container` 将 `redis` / `db_session_factory` 直接注入各服务（如 `SessionManager`），服务层拿到的是裸客户端对象。`shutdown()` 先等待 ToolService 关闭，再将 `redis.close()`、`engine.dispose()` 与 `ClientManager.close_all()` 并入 `asyncio.gather(return_exceptions=True)`。收集清理异常不等于资源均已释放，也不能证明整体优雅退出；当前缺少聊天运行及最终消息落库的完整排空边界。

**已确认的目标边界（待实施）**：先停止新业务准入，等待使用方收尾，再释放数据库；仍有使用方未结束时保留其依赖并报告关闭未完成。具体责任与验证见 [DB-ADR-001 D6](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d6生命周期接线)。

### 降级策略

`Container.initialize()` 对每个基础设施采用**独立 try / except + 置空降级**：单个资源初始化失败不影响整体启动，但会：

1. 把错误追加到 `self._errors` 列表
2. 打印 `[WARN] xxx 不可用（服务降级）`
3. 将对应属性置为 `None`

各服务需自行感知降级。例如 `SessionManager` 在 `redis is None` 时打印「Redis 不可用，缓存降级」（`session_manager.py` L42-43）。

以上描述当前行为，不是数据库目标契约。已确认的数据库目标是：初始化失败时独立 A-only 能力可继续装配，持久化消费者不装配，相关 API 明确返回 503；首次启动失败后，即使数据库恢复，也需重启应用完成装配。成功装配后的临时断连与首次启动失败分开处理，详见 [DB-ADR-001 D5](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d5公共-api-与能力契约)。

### asyncpg 驱动未安装 → DB 恒降级

这是一个**2026-09-22 再次实测确认的现状缺陷**：

- `pyproject.toml` 依赖中**没有** `asyncpg`（也没有 `psycopg` / `aiosqlite`），只有 `sqlalchemy>=2.0.51`
- `settings.database_url` 默认值为 `postgresql+asyncpg://user:pass@localhost/db`
- `create_async_engine()` 在**创建阶段**就会解析 `asyncpg` 方言并 import 驱动，驱动缺失时抛出 `ModuleNotFoundError`
- 该异常被 `Container.initialize()` 的 except 捕获 → `self._engine = None`、`self.db_session_factory = None`

因此当前**数据库连接恒降级**：即使本机有 PostgreSQL 服务，DB 持久化路径也实际不可用（`SessionManager` 等所有依赖 `db_session_factory` 的调用在运行时都会失败）。

本次核验还确认：Container 当前没有执行真实连接或最小事务，`/api/health` 也不读取基础设施状态；即使未来仅补上驱动，“引擎创建成功”与健康端点返回 `ok` 仍不能作为数据库就绪证据。共享的版本、连接、schema 与读写权限检查以及真实 PostgreSQL 迁移/事务验证，改由独立 [DB-F 任务](../todo.md#db-foundation)建设；Piece⑥消费该底座，再实现工具账本及恢复业务。

对比：`redis>=8.0.1` 已安装，Redis 连接可用性只取决于服务是否可达。

---

## 规划说明

以下为各空模块的预期功能与定位（设计蓝图，未实施）。

### database.py

**定位**：数据库访问的统一封装，替代 `container` 中的裸 `create_async_engine` 调用。

- 封装 `create_async_engine` + `async_sessionmaker` 的创建逻辑与配置（URL / 池大小 / `pool_pre_ping`）
- 沿用 `init()` / `dispose()` 生命周期命名；engine 由运行时持有，受控 `session_factory` 仅供基础设施适配器使用，Application 不接收数据库对象
- 保留轻量 `ping()` 检查真实连接与最小事务；完整 `probe()` 额外检查 schema 版本、结构和权限。两者共用检查逻辑，ping 成功不能开放持久化能力；应用存活另由 `/api/health` 表达
- 本轮只支持 PostgreSQL，不引入原蓝图候选的 SQLite/aiosqlite 降级后端

数据库运行时、统一迁移执行器和会话 Store 的职责划分见 [DB-ADR-001](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md)；该 ADR 保存决策正文，本文维护基础设施定位与协作说明。配置继续在[配置参考](../config_doc/config.md)维护，使用命令继续在[部署说明](../project/deployment.md)维护。Redis/MQ 下述规划不纳入本次数据库任务。

### redis_client.py

**定位**：Redis 客户端的统一封装，替代 `container` 中的裸 `Redis.from_url` 调用。

- 统一管理连接参数（URL / decode_responses / 连接与操作超时）
- 提供键命名空间（prefix）与常用操作的编解码封装
- 处理连接可用性检测与可选的重连策略
- 对外暴露统一 `RedisClient`，供 `SessionManager` 等缓存类服务使用

### message_queue/

**定位**：消息队列抽象，用于 Agent 任务分发与模块解耦（当前无实现，`pyproject.toml` 亦无 MQ 客户端依赖）。

- 规划抽象统一的消息发布 / 消费接口（topic 维度 publish / subscribe）
- 候选后端：进程内 `asyncio.Queue`（单机默认）、RabbitMQ（`aio-pika`）、NATS 等，由配置切换
- 支撑 `TaskService` 的任务调度与多 Agent 并发场景

---

## 相关文档链接

- [配置参考](../config_doc/config.md) — `DATABASE_URL` / `REDIS_URL` 等基础设施相关配置
- [系统架构](../project/architecture.md) — 整体架构中基础设施层的定位
- [LLM 层说明文档](../integration_doc/llm_doc/llm.md) — 同风格的分层文档参考
- [任务服务说明文档](../application_doc/task_doc/task.md) — 任务调度（潜在依赖消息队列）
