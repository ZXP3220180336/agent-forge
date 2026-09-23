# 基础设施层说明文档

> 数据库规划已按用户确认的文档对照结论收敛；当前实现状态见 [ALIGNMENT](../ALIGNMENT.md)。设计取舍及确认范围见 [DB-ADR-001](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#infrastructure-alignment)。DB-F02 已实现独立运行时，完整 schema 校验、迁移与应用装配仍待后续分片。

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
    - [驱动与数据库就绪边界](#驱动与数据库就绪边界)
  - [规划说明](#规划说明)
    - [database.py](#databasepy)
    - [database_migrations.py](#database_migrationspy)
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
├── database.py             ← DatabaseRuntime（已实现，尚未接入 Container）
├── database_migrations.py  ← 文件/历史校验及事务内迁移核心
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

> **当前状态**：ORM 模型已被 `SessionManager` 使用；`database.py` 已实现独立资源生命周期，尚未替换 Container 的直接管理；`redis_client.py` / `message_queue/` 仍为空占位。应用当前连接管理见[现状说明](#现状说明)。

---

## 模块实现状态表

| 文件 | 状态 | 定位 |
| --- | --- | --- |
| `app/infrastructure/__init__.py` | 空（0 行） | 基础设施层包入口，规划统一导出封装接口 |
| `app/infrastructure/database.py` | [见对齐表](../ALIGNMENT.md) | 独立 engine / session factory / 探测 / 有界关闭，见 [database.md](database_doc/database.md)；schema 校验与业务装配待接入 |
| `app/infrastructure/database_migrations.py` | [见对齐表](../ALIGNMENT.md) | 唯一迁移序列核心，见 [migrations.md](database_doc/migrations.md)；完整命令、首迁移与基线待后续片 |
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

### 驱动与数据库就绪边界

DB-F01 已将 asyncpg 纳入正式依赖与锁文件，驱动缺失问题的复现与修复见 [DB-001](../../issues/infrastructure/database/2026-09-22-missing-asyncpg-dependency.md)。不能继续将驱动缺失描述为当前必然降级原因；实际模块与验证状态见 [ALIGNMENT](../ALIGNMENT.md)。

Container 仍只构造 engine/sessionmaker，没有真实连接、schema 或权限检查；`/api/health` 仍不能证明数据库就绪。空工厂消费者、虚假可用性和空迁移 CLI 已有严格预期失败测试，分别由 DB-F02/03/05 继续闭合，不因安装驱动就宣称持久化可用。

数据库预算字段与消费阶段由[配置参考](../config_doc/config.md#7-数据库配置)统一定义，DB-F02 独立运行时已消费连接、取池、探测、清理和关闭预算，现有 Container 尚未改造。统一迁移及 PostgreSQL 验收归独立 [DB-F 任务](../todo.md#db-foundation)；Piece⑥消费底座，再实现工具账本与恢复业务。

---

## 规划说明

数据库运行时的接口与内部协作契约移至组件文档，本节只保留进入本层维护的边界与待接线范围；Redis/MQ 部分仍为设计蓝图。

### database.py

**定位**：数据库资源唯一 Owner，替代 Container 的直接创建（归 DB-F05），不在应用启动时自动迁移。DB-F02 已实现独立运行时；生产 schema 检查器、迁移与应用装配仍待后续分片。

准入状态机、探测与关闭流程、资源责任、并发/取消边界与原因码分类见组件文档 [database.md](database_doc/database.md)。进入本层维护的边界：

- 输入为 Settings 已校验的连接/池参数与不可变 `DatabaseTimeouts`；六项超时通过 `timeouts` 聚合传入，不接受 migration 两项预算。`database_config` 含迁移参数与凭证，不能整体展开或日志化，完整键表见[配置参考](../config_doc/config.md#7-数据库配置)。
- 完整 schema 校验经受信只读异步回调接入，缺失时即使 `SELECT 1` 成功也不开放工厂；当前生产装配不存在该检查器。
- 仍有 Session 或业务连接 Owner 时不 dispose、不强关；消费方 drain 与「首次启动失败需重启」的装配语义归 DB-F05。
- PostgreSQL / asyncpg 是唯一后端，不引入降级后端。

数据库运行时、统一迁移执行器和会话 Store 的职责划分见 [DB-ADR-001](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md)；该 ADR 保存决策正文，本文维护基础设施定位与协作说明。配置继续在[配置参考](../config_doc/config.md)维护，使用命令继续在[部署说明](../project/deployment.md)维护。

### database_migrations.py

**定位**：统一迁移序列的文件与全历史校验、只读版本核验和单文件事务内执行。内部调用顺序、期限及结算责任见 [migrations.md](database_doc/migrations.md)。核心借用调用方连接，不另建池；完整命令 Owner 归 DB-F03b，版本表结构、首迁移与基线归 DB-F04。不能把本核心单独接为完整 readiness 检查器或可用迁移命令。

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

- [数据库运行时（database.py）](database_doc/database.md) — 组件级内部协作契约与维护说明
- [数据模型层说明](model_doc/model.md) — ORM 模型、会话与消息表契约
- [配置参考](../config_doc/config.md) — `DATABASE_URL` / `REDIS_URL` 等基础设施相关配置
- [系统架构](../project/architecture.md) — 整体架构中基础设施层的定位
- [LLM 层说明文档](../integration_doc/llm_doc/llm.md) — 同风格的分层文档参考
- [任务服务说明文档](../application_doc/task_doc/task.md) — 任务调度（潜在依赖消息队列）
