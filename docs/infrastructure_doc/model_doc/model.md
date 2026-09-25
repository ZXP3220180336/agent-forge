# 数据模型层说明文档

## 📋 目录

- [数据模型层说明文档](#数据模型层说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [已实现模型详解](#已实现模型详解)
    - [Base — ORM 基类](#base--orm-基类)
    - [SessionModel — 会话](#sessionmodel--会话)
    - [MessageModel — 消息](#messagemodel--消息)
  - [模型使用方式](#模型使用方式)
  - [预留模型说明](#预留模型说明)
    - [database/task.py — 任务表](#databasetaskpy--任务表)
    - [database/tool\_log.py — 工具调用日志表](#databasetool_logpy--工具调用日志表)
    - [Pydantic Schema（API 层）](#pydantic-schemaapi-层)
  - [设计注意](#设计注意)
    - [约定小结](#约定小结)
  - [当前状态与遗留](#当前状态与遗留)
  - [常见问题](#常见问题)
    - [Q: 为什么会话 id 用 UUID 字符串，消息 id 用自增 BigInteger？](#q-为什么会话-id-用-uuid-字符串消息-id-用自增-biginteger)
    - [Q: `updated_at` 什么时候被刷新？](#q-updated_at-什么时候被刷新)
    - [Q: 为什么 `reasoning_content` 单独存一列，且读历史时不返回？](#q-为什么-reasoning_content-单独存一列且读历史时不返回)
    - [Q: 为什么用 `meta` JSON 而不是直接加列？](#q-为什么用-meta-json-而不是直接加列)
    - [Q: 什么时候用软删除，什么时候用物理删除？](#q-什么时候用软删除什么时候用物理删除)
    - [Q: 新增一张表要注意什么？](#q-新增一张表要注意什么)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

`app/infrastructure/models/` 是系统的**ORM 数据模型层**，承载所有结构化数据的持久化定义：

- **ORM 模型（`database/` 子包）**：SQLAlchemy 声明式模型，与数据库表一一对应，负责持久化会话与消息
- **Pydantic Schema（`app/api/schemas/`）**：请求 / 响应数据校验模型，负责 API 层的出入参校验（不属于本层，见 [routes.md](../../api_doc/routes_doc/routes.md)）

其中 ORM 模型已完成，是会话管理与消息持久化的基石。

### 模块结构

```
app/infrastructure/models/
├── __init__.py              ← 模块入口，导出 MessageModel / SessionModel
└── database/                ← SQLAlchemy ORM 子包
    ├── __init__.py          ← 导出 Base / MessageModel / SessionModel
    ├── base.py              ← Base（共享 declarative_base 实例）
    ├── session.py           ← SessionModel（会话表）
    ├── messages.py          ← MessageModel（消息表）
    ├── task.py              ← ⏳ 预留：任务表
    └── tool_log.py          ← ⏳ 预留：工具调用日志表

app/api/schemas/             ← Pydantic 模型（API 层，见 routes.md）
    ├── request.py           ← 请求体模型（已实现）
    ├── response.py          ← 响应体模型（已实现）
    └── agent.py             ← 预留：Agent 相关数据结构
```

### 设计原则

1. **ORM 与 Schema 分层**：`database/` 管数据库映射，`app/api/schemas/` 管 API 校验，两者互不混用（Pydantic 模型不直接作为 ORM 使用）
2. **单一 Base**：所有模型共享 `database/base.py` 中唯一一个 `declarative_base()` 实例（详见「设计注意与历史教训」）
3. **JSON 扩展字段**：`meta` 列承载未定型的扩展数据，避免频繁改动表结构
4. **导出收敛**：外部只从 `app.infrastructure.models` / `app.infrastructure.models.database` 导入模型，不直接 import 具体文件

### 依赖关系

```
服务层（SessionManager 等）
        │
        ▼
app.infrastructure.models（__init__.py）
        │
        └── database/__init__.py
              ├── base.py      ← Base
              ├── session.py   ← SessionModel
              └── messages.py  ← MessageModel（FK → public.sessions.id）

API 层（schemas）
        ▼
app.api.schemas/（Pydantic，见 routes.md）
```

---

## 已实现模型详解

### Base — ORM 基类

**文件**：`app/infrastructure/models/database/base.py`（8 行）

```python
from sqlalchemy.orm import declarative_base

Base = declarative_base()
```

#### 功能

- 全局唯一一个 `declarative_base()` 实例，所有表模型统一继承
- 全项目模型共享同一个 `metadata`，保证跨表外键（FK）引用在同一个映射空间内注册

#### 为什么独立成文件

共享 `Base` 独立于各模型文件声明（`declarative_base()` 单例），所有模型 `from .base import Base` 继承——保证 FK 引用在单一映射空间内注册。设计过程见 [lessons.md](../../lessons.md)「两个 declarative_base() 实例」。

---

### SessionModel — 会话

**文件**：`app/infrastructure/models/database/session.py`，表名 `public.sessions`。模型明确声明 `schema="public"`，避免 `search_path` 中的同名表改变实际读写目标。

会话是**多轮对话的基本单位**：一个会话绑定一个用户、一组历史消息和一段系统提示词。`SessionManager` 围绕它做创建 / 查询 / 列表 / 删除（软删）等操作，并通过 Redis 缓存热会话。

#### 字段表

| 字段            | 类型                        | 约束 / 默认                     | 说明                                |
| --------------- | --------------------------- | ------------------------------- | ----------------------------------- |
| `id`            | `String(36)`                | 主键                            | 会话 UUID（`uuid.uuid4()` 生成）    |
| `user_id`       | `String(64)`                | NOT NULL，索引                  | 所属用户 ID，鉴权隔离的依据         |
| `title`         | `String(200)`               | 默认 `"新对话"`                 | 会话标题                            |
| `system_prompt` | `Text`                      | 默认 `"你是一个友好的AI助手"`   | 系统提示词，驱动 Agent 行为         |
| `created_at`    | `DateTime(timezone=True)`   | `default=lambda: datetime.now(UTC)` | 每次插入执行时取 UTC 时间         |
| `updated_at`    | `DateTime(timezone=True)`   | `onupdate=lambda: datetime.now(UTC)` | 更新时取 UTC 时间，无插入默认    |
| `status`        | `String(20)`                | 默认 `"active"`                 | `active` / `archived` / `deleted`   |
| `meta`          | `JSON`                      | `default=dict`                 | 每次执行创建独立字典，扩展字段     |

#### 设计说明

- **UUID 主键**：会话 ID 对外暴露（API 路径 / Redis key），用 UUID 避免可枚举与碰撞；`user_id` 加索引支撑「按用户查会话列表」
- **`status` 软删除**：`delete_session()` 只把 `status` 置为 `deleted`，`list_sessions()` 过滤 `status == "active"`；物理删除仅由 `hard_delete_session()` 执行（管理员 / 定时任务）
- **`meta` JSON**：存放暂不定型的扩展数据，避免加列迁移；`SessionManager` 在 Redis 缓存中维护 `message_count` / `total_tokens` 等统计，不落库

---

### MessageModel — 消息

**文件**：`app/infrastructure/models/database/messages.py`，表名 `public.messages`。模型明确声明 `schema="public"`，外键目标为 `public.sessions.id`。

消息是**每一轮对话的持久化记录**，外键关联会话。`SessionManager.get_messages()` 读取历史喂给 Agent，`add_message()` 写入每一轮交互。

#### 字段表

| 字段                | 类型                      | 约束 / 默认                   | 说明                                    |
| ------------------- | ------------------------- | ----------------------------- | --------------------------------------- |
| `id`                | `BigInteger`              | 主键，自增                    | 自增主键，内部引用                       |
| `session_id`        | `String(36)`              | FK → `public.sessions.id`，索引 | 所属会话，保持非级联外键                |
| `role`              | `String(20)`              | NOT NULL                      | `system` / `user` / `assistant`         |
| `content`           | `Text`                    | NOT NULL                      | 消息内容                                |
| `reasoning_content` | `Text`                    | 可空                          | 思考过程（**不进入历史**，见下）        |
| `token_count`       | `Integer`                 | 默认 `0`                      | 消息 Token 数，用于成本与上下文统计     |
| `created_at`        | `DateTime(timezone=True)` | `default=lambda: datetime.now(UTC)` | 每次插入执行时取 UTC 时间         |
| `meta`              | `JSON`                    | `default=dict`               | 每次执行创建独立字典                    |

#### 设计说明

- **自增主键**：消息量大、纯内部使用，自增 `BigInteger` 高效；`session_id` 索引支撑「按会话查历史」
- **`reasoning_content` 独立存储**：推理模型的思考过程单独落列，`get_messages()` 只取 `role in ("user", "assistant")` 且不返回该列，**避免把思考过程回灌给模型**
- **软删依赖顺序**：物理删除时必须先删 `messages` 再删 `sessions`（FK 约束），`hard_delete_session()` 即按此顺序执行

---

## 模型使用方式

当前唯一的模型使用方是 `SessionManager`（`app/application/session/session_manager.py`），它把 **Redis 热缓存 + SQLAlchemy 持久化**组合使用。关键使用点：

| 操作             | 使用模型                         | 要点                                                              |
| ---------------- | -------------------------------- | ----------------------------------------------------------------- |
| `create_session` | `insert(SessionModel)`           | 主键由 `uuid.uuid4()` 生成；写入 DB 后预热 Redis 缓存             |
| `get_session`    | `select(SessionModel)`           | 先查 Redis，未命中再查 DB 并回写缓存（缓存穿透保护）              |
| `get_messages`   | `select(MessageModel)`           | 仅 `role in ("user", "assistant")`，按 `created_at` 升序，分页    |
| `add_message`    | `insert(MessageModel)`           | 写入 role / content / reasoning_content / token_count             |
| `delete_session` | `update(SessionModel)`           | 软删除：`status="deleted"` + 手动刷新 `updated_at`                |
| `hard_delete_session` | `delete(MessageModel)` + `delete(SessionModel)` | 先删消息再删会话（FK 约束）                               |
| `list_sessions`  | `select(SessionModel)`           | 按 `user_id` + `status="active"` 过滤，`updated_at` 降序分页      |
| `list_sessions_v2` | 同上 + 条件查询                  | 支持关键词 / 日期 / 排序 / 状态筛选，额外返回总数                 |
| `_get_session_stats` | `func.count` / `func.sum` / `func.max` 聚合 | 统计消息数、Token 总数、最后消息时间，结果缓存 60 秒    |

> **注意**：通过 SQLAlchemy 模型构造 UPDATE 且未显式提供 `updated_at` 时，客户端 `onupdate` 才会求值。软删除显式提供时间时使用该值。直接 SQL 不执行此 Python 默认逻辑。

---

## 预留模型说明

以下 `database/` 下的文件当前为空（❌ 空文件，预留待实现），说明其预期用途，避免后续重复造轮子。

### database/task.py — 任务表

**状态**：❌ 空文件（0 行），预留。

任务表用于持久化 **Agent 任务的执行记录**。内存态任务结构已定义在 `app/application/task/task_service.py` 对应的 `Task` 数据结构中（见 [task.md](../../application_doc/task_doc/task.md)「数据模型」小节），含 `task_id` / `user_request` / `priority` / `status` / `parent_task_id` / `sub_tasks` / `agent_result` / `error` / `created_at` / `started_at` / `completed_at` 等字段。

**预期用途**：

- 任务失败后重启恢复（从 DB 重新拉起未完成任务）
- 跨进程 / 多节点共享任务状态（当前 `TaskService` 仅进程内信号量限流）
- 任务审计与统计分析（耗时、成功率、Token 消耗）

> 注意：任务结构目前以内存 `@dataclass` 承载，是否落库、落库字段与 `meta` 如何划分，需在实现时与 `task_doc/task.md` 对齐后决策。

### database/tool_log.py — 工具调用日志表

**状态**：❌ 空文件（0 行），预留。

工具调用日志表用于持久化**每次工具调用的审计记录**。工具系统的统一抽象见 [tools.md](../../integration_doc/tools_doc/tools.md)（`BaseTool` / `ToolResult`，10 个内置工具）。

**预期用途**：

- 记录工具名、入参、出参、耗时、是否成功、归属会话 / 任务
- 安全审计与成本归因（工具调用往往伴随 Token 消耗）
- 工具可靠性统计（哪些工具失败率高，辅助改进）

### Pydantic Schema（API 层）

Pydantic Schema 位于 **`app/api/schemas/`**（不属于本层），用于 API 层出入参的校验与文档化，详见 [routes.md](../../api_doc/routes_doc/routes.md)：

| 文件 | 状态 | 用途 |
| -------------- | -------------------------------------- | -------------------------------------- |
| `request.py` | [见对齐表](../../ALIGNMENT.md) | 请求体模型（如创建会话、发送消息） |
| `response.py` | [见对齐表](../../ALIGNMENT.md) | 响应体模型（如会话详情、消息列表） |
| `agent.py` | [见对齐表](../../ALIGNMENT.md) | Agent 相关数据结构（ReAct 结果等） |

---

## 设计注意

> 本项目 ORM 建模的约定与历史问题详见 [lessons.md](../../lessons.md)（两个 declarative_base / metadata 保留字），本文只列当前约定。

### 约定小结

- 所有 ORM 模型继承 `database/base.py` 的共享 `Base`
- 扩展字段一律叫 `meta`（JSON），不用保留字 `metadata`
- 时间字段用 `DateTime(timezone=True)` + UTC；`created_at` 的 `default` 和 `updated_at` 的 `onupdate` 都传入 callable，在执行时求值；`meta` 使用 `dict` 工厂
- Session/Message 及其外键明确指向 `public`，与结构检查和迁移目标一致
- 对外导出走 `app/infrastructure/models/__init__.py` 与 `app/infrastructure/models/database/__init__.py`

首迁移 [0001_sessions_and_messages.sql](../../../migrations/0001_sessions_and_messages.sql) 保留 ORM 的 VARCHAR 长度、JSON、带时区时间、可空性、索引及非级联外键；消息主键使用 BIGSERIAL 和所属序列。上述 Python 默认不是 `server_default`：直接 SQL 省略可空列时仍得到 NULL，`updated_at` 也没有插入默认或数据库触发器。建表和旧库接管只走[迁移入口](../database_doc/migrations.md)，应用启动不调用 `create_all`。

验证入口：[默认参数单测](../../../tests/unit/test_database_model_defaults.py) 使用 PostgreSQL 编译器及 SQLAlchemy 默认参数处理器，检查逐次时间求值和字典对象隔离；[真实 PostgreSQL 模型测试](../../../tests/integration/test_database_models.py) 核对 ORM DDL 与首迁移 catalog 契约、两个时刻的写入、直接 SQL 默认行为，以及同名临时表不会重定向 ORM 读写。真实测试配置与运行方式见[部署文档](../../project/deployment.md)，结果状态以[对齐表](../../ALIGNMENT.md)为准。

---

## 当前状态与遗留

实现、接线和测试状态统一见[对齐表](../../ALIGNMENT.md)。`asyncpg` 已纳入正式依赖；DB-F04 修复了 import 时固定时间及共享字典默认，首迁移与模型在专用 PostgreSQL 测试库核验。模型和迁移验收不代表应用启动、Store 或 readiness 已接线，这些后续工作由[当前计划](../../todo.md)管理。

`task.py`、`tool_log.py` 仍是预留位置，不包含在本次首迁移中；出现已批准的持久化需求后再定义表和领取后续迁移版本。

---

## 常见问题

### Q: 为什么会话 id 用 UUID 字符串，消息 id 用自增 BigInteger？

两者定位不同：

- **会话 ID** 对外暴露（API 路径、Redis key），UUID 无法枚举、不易碰撞，且天然适合作为分布式缓存 key
- **消息 ID** 纯内部使用，量级大，自增 `BigInteger` 插入高效、索引紧凑；用户不直接引用消息 ID

### Q: `updated_at` 什么时候被刷新？

通过 `onupdate=lambda: datetime.now(UTC)`，在 SQLAlchemy 模型 UPDATE 未显式提供该列时，按本次执行时刻生成 UTC 时间。显式提供的时间优先；直接 SQL 不触发客户端默认，插入省略 `updated_at` 时仍为 NULL。

### Q: 为什么 `reasoning_content` 单独存一列，且读历史时不返回？

推理模型的思考过程（如 DeepSeek-R1 的 reasoning）若回灌给模型，会导致上下文膨胀、重复推理、成本上升。`SessionManager.get_messages()` 只取 `role in ("user", "assistant")` 的消息且不读该列，保证喂给 Agent 的历史干净。

### Q: 为什么用 `meta` JSON 而不是直接加列？

`meta` 用于**暂不定型**的扩展数据。扩展字段列统一用 `meta`（`metadata` 是 SQLAlchemy 保留字，不能作列名，见 [lessons.md](../../lessons.md)）。若某字段长期稳定且需要查询过滤，应升级为正式列并加索引，而不是塞进 JSON。

### Q: 什么时候用软删除，什么时候用物理删除？

- **软删除（默认）**：`delete_session()` 把 `status` 置为 `deleted`，数据保留可恢复、可审计
- **物理删除**：`hard_delete_session()` 先删消息再删会话（FK 约束顺序），仅管理员 / 定时任务清理用

### Q: 新增一张表要注意什么？

1. 继承 `database/base.py` 的共享 `Base`（唯一 `declarative_base()` 实例）
2. 列名避开保留字（`metadata` 等），扩展字段用 `meta`
3. 在 `app/infrastructure/models/database/__init__.py` 中导出（若对外使用还需在 `app/infrastructure/models/__init__.py` 导出）
4. 时间字段统一 UTC + `DateTime(timezone=True)`

---

## 相关文档

| 文档                                                 | 关联内容                                           |
| ----------------------------                         | --------------------------------------------       |
| [架构总览](../../project/architecture.md)                    | 数据模型层的分层定位与整体架构                     |
| [研发教训](../../lessons.md)                         | 项目级研发教训（metadata / declarative_base 出处） |
| [任务模块](../../application_doc/task_doc/task.md)   | `task.py` 预留对应的任务数据结构                   |
| [工具系统](../../integration_doc/tools_doc/tools.md) | `tool_log.py` 预留对应的工具抽象与内置工具         |
| [路由模块](../../api_doc/routes_doc/routes.md)                       | `schemas/` 预留对应的请求 / 响应模型               |
| [配置参考](../../config_doc/config.md)               | `DATABASE_URL` 等数据库连接配置                    |
| [部署文档](../../project/deployment.md)                      | 数据库部署与 `asyncpg` 依赖说明                    |
