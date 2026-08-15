# 基础设施层说明文档

## 目录

- [模块概述](#模块概述)
- [模块实现状态表](#模块实现状态表)
- [现状说明](#现状说明)
  - [DB / Redis 由 container 直接管理](#db--redis-由-container-直接管理)
  - [降级策略](#降级策略)
  - [asyncpg 驱动未安装 → DB 恒降级](#asyncpg-驱动未安装--db-恒降级)
- [规划说明](#规划说明)
  - [database.py](#databasepy)
  - [redis_client.py](#redis_clientpy)
  - [vector_store/](#vector_store)
  - [message_queue/](#message_queue)
- [相关文档链接](#相关文档链接)

---

## 模块概述

基础设施层（`app/infrastructure/`）是系统的**底层资源抽象层**，负责对数据库、缓存、向量存储、消息队列等外部基础设施进行统一封装，向上层服务提供稳定的访问接口。

### 核心定位

- **抽象封装**：屏蔽具体技术细节（驱动、连接池、协议），上层只依赖本层暴露的接口
- **生命周期管理**：统一负责资源的创建、初始化、健康检查与释放
- **可替换性**：通过接口隔离实现可替换（如向量库在 Milvus / Qdrant / Pinecone 之间切换）
- **解耦**：让服务层不再直接持有具体客户端对象

### 模块结构

```
app/infrastructure/
├── __init__.py             ← 包入口，规划导出统一封装接口
├── database.py             ← 数据库封装（规划：engine / session factory）
├── redis_client.py         ← Redis 封装（规划：连接池 / 编解码 / 重连）
├── vector_store/           ← 向量存储子包
│   ├── __init__.py         ← 子包入口，规划按配置选择实现
│   ├── base.py             ← 向量存储抽象基类
│   └── milvus.py           ← Milvus 实现
└── message_queue/          ← 消息队列子包
    └── __init__.py         ← 子包入口
```

> **当前状态**：本层所有文件均为空占位，尚未提供实际封装。DB / Redis 实际由 `app/container.py` 直接管理（见 [现状说明](#现状说明)）。

---

## 模块实现状态表

| 文件 | 状态 | 定位 |
| --- | --- | --- |
| `app/infrastructure/__init__.py` | 空（0 行） | 基础设施层包入口，规划统一导出封装接口 |
| `app/infrastructure/database.py` | 空（0 行） | 数据库引擎与会话封装（engine / session factory / 生命周期 / 健康检查） |
| `app/infrastructure/redis_client.py` | 空（0 行） | Redis 客户端封装（连接池 / 编解码 / 超时 / 重连 / 命名空间） |
| `app/infrastructure/vector_store/__init__.py` | 空（0 行） | 向量存储子包入口，规划工厂方法按配置选择实现 |
| `app/infrastructure/vector_store/base.py` | 空（0 行） | 向量存储抽象基类（集合管理 / 写入 / 检索 / 删除的统一接口） |
| `app/infrastructure/vector_store/milvus.py` | 空（0 行） | Milvus 实现（对应配置 `memory_vector_db="milvus"`） |
| `app/infrastructure/message_queue/__init__.py` | 空（0 行） | 消息队列子包入口，规划抽象统一消息发布 / 消费接口 |

---

## 现状说明

### DB / Redis 由 container 直接管理

当前基础设施资源**未经过** `infrastructure/` 层封装，而是由 `app/container.py` 的 `Container.initialize()` 直接创建：

**数据库**（`container.py` L74-93）：

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

**Redis**（`container.py` L59-72）：

```python
self.redis = Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
)
await self.redis.ping()
```

**调用链**：`Container` 将 `redis` / `db_session_factory` 直接注入各服务（如 `SessionManager`），服务层拿到的是裸客户端对象，而非经过封装的统一接口。`shutdown()` 时通过 `redis.close()`、`engine.dispose()` 与 `ClientManager.close_all()`（关闭 AsyncOpenAI 底层 httpx 连接池，2026-08-09）显式释放，三者并入 `asyncio.gather(return_exceptions=True)`——单个清理失败不影响整体优雅退出。

### 降级策略

`Container.initialize()` 对每个基础设施采用**独立 try / except + 置空降级**：单个资源初始化失败不影响整体启动，但会：

1. 把错误追加到 `self._errors` 列表
2. 打印 `[WARN] xxx 不可用（服务降级）`
3. 将对应属性置为 `None`

各服务需自行感知降级。例如 `SessionManager` 在 `redis is None` 时打印「Redis 不可用，缓存降级」（`session_manager.py` L42-43）。

### asyncpg 驱动未安装 → DB 恒降级

这是一个**已确认的现状缺陷**：

- `pyproject.toml` 依赖中**没有** `asyncpg`（也没有 `psycopg` / `aiosqlite`），只有 `sqlalchemy>=2.0.51`
- `settings.database_url` 默认值为 `postgresql+asyncpg://user:pass@localhost/db`
- `create_async_engine()` 在**创建阶段**就会解析 `asyncpg` 方言并 import 驱动，驱动缺失时抛出 `ModuleNotFoundError`
- 该异常被 `Container.initialize()` 的 except 捕获 → `self._engine = None`、`self.db_session_factory = None`

因此当前**数据库连接恒降级**：即使本机有 PostgreSQL 服务，DB 持久化路径也实际不可用（`SessionManager` 等所有依赖 `db_session_factory` 的调用在运行时都会失败）。

对比：`redis>=8.0.1` 已安装，Redis 连接可用性只取决于服务是否可达。

---

## 规划说明

以下为各空模块的预期功能与定位（设计蓝图，未实施）。

### database.py

**定位**：数据库访问的统一封装，替代 `container` 中的裸 `create_async_engine` 调用。

- 封装 `create_async_engine` + `async_sessionmaker` 的创建逻辑与配置（URL / 池大小 / `pool_pre_ping`）
- 提供 `engine` / `session_factory` 属性与 `init()` / `dispose()` 生命周期方法
- 提供健康检查（`ping` 能力），供 `Container` 与监控复用
- **可选**：增加本地降级后端（如 `aiosqlite`），解除「asyncpg 未装 → 恒降级」的现状

### redis_client.py

**定位**：Redis 客户端的统一封装，替代 `container` 中的裸 `Redis.from_url` 调用。

- 统一管理连接参数（URL / decode_responses / 连接与操作超时）
- 提供键命名空间（prefix）与常用操作的编解码封装
- 处理连接可用性检测与可选的重连策略
- 对外暴露统一 `RedisClient`，供 `SessionManager` 等缓存类服务使用

### vector_store/

**定位**：向量存储抽象层，支撑记忆系统（`settings.memory_vector_db="milvus"`）。

- `base.py`：定义抽象基类，统一接口 —— 集合管理（create / drop / 集合信息）、写入（单条 / 批量）、检索（top-k 相似度搜索）、删除；约定向量维度与距离度量（metric）的配置传入方式
- `milvus.py`：基于 `pymilvus` 的 Milvus 实现，负责集合 schema、索引构建与检索细节
- `__init__.py`：提供工厂方法，根据 `memory_vector_db` 配置返回对应实现（Milvus / Qdrant / Pinecone），上层不感知具体向量库

### message_queue/

**定位**：消息队列抽象，用于 Agent 任务分发与模块解耦（当前无实现，`pyproject.toml` 亦无 MQ 客户端依赖）。

- 规划抽象统一的消息发布 / 消费接口（topic 维度 publish / subscribe）
- 候选后端：进程内 `asyncio.Queue`（单机默认）、RabbitMQ（`aio-pika`）、NATS 等，由配置切换
- 支撑 `TaskService` 的任务调度与多 Agent 并发场景

---

## 相关文档链接

- [配置参考](../config_doc/config.md) — `DATABASE_URL` / `REDIS_URL` / `memory_vector_db` 等基础设施相关配置
- [系统架构](../architecture.md) — 整体架构中基础设施层的定位
- [项目整体进度](../HANDOFF.md) — 项目全局状态与待办
- [LLM 层说明文档](../integration_doc/llm_doc/llm.md) — 同风格的分层文档参考
- [任务服务说明文档](../application_doc/task_doc/task.md) — 任务调度（潜在依赖消息队列）
- [记忆系统说明文档](../domain_doc/memory_doc/memory.md) — 依赖 `vector_store` 的记忆实现（目录当前为空）
