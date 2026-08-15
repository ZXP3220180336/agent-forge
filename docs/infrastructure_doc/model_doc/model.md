# 数据模型层说明文档

## 📋 目录

- [模块概述](#模块概述)
- [已实现模型详解](#已实现模型详解)
  - [Base — ORM 基类](#base--orm-基类)
  - [SessionModel — 会话](#sessionmodel--会话)
  - [MessageModel — 消息](#messagemodel--消息)
- [模型使用方式](#模型使用方式)
- [预留模型说明](#预留模型说明)
  - [database/task.py — 任务表](#databasetaskpy--任务表)
  - [database/tool_log.py — 工具调用日志表](#databasetool_logpy--工具调用日志表)
  - [schemas/ — Pydantic 模型](#schemas--pydantic-模型)
- [设计注意与历史教训](#设计注意与历史教训)
  - [两个 declarative_base() 实例](#两个-declarative_base-实例)
  - [SQLAlchemy 保留属性 metadata](#sqlalchemy-保留属性-metadata)
- [当前状态与遗留](#当前状态与遗留)
- [常见问题](#常见问题)
- [相关文档](#相关文档)

---

## 模块概述

### 核心功能

`app/infrastructure/models/` 是系统的**数据模型层**，承载所有结构化数据的定义，职责分两部分：

- **ORM 模型（`database/` 子包）**：SQLAlchemy 声明式模型，与数据库表一一对应，负责持久化会话与消息
- **Pydantic Schema（`schemas/` 子包）**：请求 / 响应数据校验模型，负责 API 层的出入参校验（**当前为空，预留**）

其中 ORM 模型已完成，是会话管理与消息持久化的基石；Schema 层随 API 层落地后填充。

### 模块结构

```
app/infrastructure/models/
├── __init__.py              ← 模块入口，导出 MessageModel / SessionModel
├── database/                ← SQLAlchemy ORM 子包
│   ├── __init__.py          ← 导出 Base / MessageModel / SessionModel
│   ├── base.py              ← Base（共享 declarative_base 实例）
│   ├── session.py           ← SessionModel（会话表）
│   ├── messages.py          ← MessageModel（消息表）
│   ├── task.py              ← ⏳ 预留：任务表
│   └── tool_log.py          ← ⏳ 预留：工具调用日志表
└── schemas/                 ← Pydantic 模型（预留，全部为空文件）
    ├── __init__.py
    ├── request.py           ← 预留：请求体模型
    ├── response.py          ← 预留：响应体模型
    └── agent.py             ← 预留：Agent 相关数据结构
```

### 设计原则

1. **ORM 与 Schema 分层**：`database/` 管数据库映射，`schemas/` 管 API 校验，两者互不混用（Pydantic 模型不直接作为 ORM 使用）
2. **单一 Base**：所有模型共享 `database/base.py` 中唯一一个 `declarative_base()` 实例（详见「设计注意与历史教训」）
3. **JSON 扩展字段**：`meta` 列承载未定型的扩展数据，避免频繁改动表结构
4. **导出收敛**：外部只从 `app.models` / `app.models.database` 导入模型，不直接 import 具体文件

### 依赖关系

```
服务层（SessionManager 等）
        │
        ▼
app.models（__init__.py）
        │
        └── database/__init__.py
              ├── base.py      ← Base
              ├── session.py   ← SessionModel
              └── messages.py  ← MessageModel（FK → sessions.id）

API 层（预留）
        ▼
app.models.schemas/（Pydantic，预留）
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

历史上 `messages.py` 和 `session.py` 各自声明 `Base = declarative_base()`，导致 FK 引用时 mapper 冲突（详见「设计注意与历史教训」）。独立成 `base.py` 并让所有模型 `from .base import Base`，从源头避免多 Base 问题。

---

### SessionModel — 会话

**文件**：`app/infrastructure/models/database/session.py`（23 行），表名 `sessions`

会话是**多轮对话的基本单位**：一个会话绑定一个用户、一组历史消息和一段系统提示词。`SessionManager` 围绕它做创建 / 查询 / 列表 / 删除（软删）等操作，并通过 Redis 缓存热会话。

#### 字段表

| 字段            | 类型                        | 约束 / 默认                     | 说明                                |
| --------------- | --------------------------- | ------------------------------- | ----------------------------------- |
| `id`            | `String(36)`                | 主键                            | 会话 UUID（`uuid.uuid4()` 生成）    |
| `user_id`       | `String(64)`                | NOT NULL，索引                  | 所属用户 ID，鉴权隔离的依据         |
| `title`         | `String(200)`               | 默认 `"新对话"`                 | 会话标题                            |
| `system_prompt` | `Text`                      | 默认 `"你是一个友好的AI助手"`   | 系统提示词，驱动 Agent 行为         |
| `created_at`    | `DateTime(timezone=True)`   | 默认 `datetime.now(UTC)`        | 创建时间（UTC）                     |
| `updated_at`    | `DateTime(timezone=True)`   | `onupdate=datetime.now(UTC)`    | 更新时间（更新时自动刷新，可手动置） |
| `status`        | `String(20)`                | 默认 `"active"`                 | `active` / `archived` / `deleted`   |
| `meta`          | `JSON`                      | 默认 `{}`                       | 扩展字段（如缓存 Token 统计）       |

#### 设计说明

- **UUID 主键**：会话 ID 对外暴露（API 路径 / Redis key），用 UUID 避免可枚举与碰撞；`user_id` 加索引支撑「按用户查会话列表」
- **`status` 软删除**：`delete_session()` 只把 `status` 置为 `deleted`，`list_sessions()` 过滤 `status == "active"`；物理删除仅由 `hard_delete_session()` 执行（管理员 / 定时任务）
- **`meta` JSON**：存放暂不定型的扩展数据，避免加列迁移；`SessionManager` 在 Redis 缓存中维护 `message_count` / `total_tokens` 等统计，不落库

---

### MessageModel — 消息

**文件**：`app/infrastructure/models/database/messages.py`（30 行），表名 `messages`

消息是**每一轮对话的持久化记录**，外键关联会话。`SessionManager.get_messages()` 读取历史喂给 Agent，`add_message()` 写入每一轮交互。

#### 字段表

| 字段                | 类型                      | 约束 / 默认                   | 说明                                    |
| ------------------- | ------------------------- | ----------------------------- | --------------------------------------- |
| `id`                | `BigInteger`              | 主键，自增                    | 自增主键，内部引用                       |
| `session_id`        | `String(36)`              | FK → `sessions.id`，索引      | 所属会话                                |
| `role`              | `String(20)`              | NOT NULL                      | `system` / `user` / `assistant`         |
| `content`           | `Text`                    | NOT NULL                      | 消息内容                                |
| `reasoning_content` | `Text`                    | 可空                          | 思考过程（**不进入历史**，见下）        |
| `token_count`       | `Integer`                 | 默认 `0`                      | 消息 Token 数，用于成本与上下文统计     |
| `created_at`        | `DateTime(timezone=True)` | 默认 `datetime.now(UTC)`      | 创建时间（UTC）                         |
| `meta`              | `JSON`                    | 默认 `{}`                     | 扩展字段                                |

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

> **注意**：`updated_at` 通过 `onupdate` 自动刷新，但软删除（`delete_session`）是显式 UPDATE，因此代码中手动设置了 `updated_at=datetime.now(UTC)`，两者并不冲突。

---

## 预留模型说明

以下文件当前为空（✅ 无内容，⏳ 预留待实现），说明其预期用途，避免后续重复造轮子。

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

工具调用日志表用于持久化**每次工具调用的审计记录**。工具系统的统一抽象见 [tools.md](../../integration_doc/tools_doc/tools.md)（`BaseTool` / `ToolResult`，5 个内置工具）。

**预期用途**：
- 记录工具名、入参、出参、耗时、是否成功、归属会话 / 任务
- 安全审计与成本归因（工具调用往往伴随 Token 消耗）
- 工具可靠性统计（哪些工具失败率高，辅助改进）

### schemas/ — Pydantic 模型

**状态**：❌ 全部空文件（`__init__.py` / `request.py` / `response.py` / `agent.py`）。

Pydantic Schema 用于 **API 层出入参的校验与文档化**：

| 文件           | 预期用途                                   |
| -------------- | ------------------------------------------ |
| `request.py`   | 请求体模型（如创建会话、发送消息、任务下发） |
| `response.py`  | 响应体模型（如会话详情、消息列表、分页包装） |
| `agent.py`     | Agent 相关数据结构（ReAct 结果、事件负载等） |

**预期用途**：
- FastAPI 路由参数校验（`Depends` + Body 校验）
- OpenAPI 自动文档（`/docs` 交互式接口）
- 服务层与 API 层的解耦（服务层返回 dict / dataclass，API 层用 Schema 包装）

---

## 设计注意与历史教训

> 以下两条为项目级研发教训中标记待数据模型文档的条目（原 `⏳` 标记，本处正式收录，出处见 [lessons.md](../../lessons.md)）。

### 两个 declarative_base() 实例

**问题**：早期 `messages.py` 与 `session.py` **各自**执行 `Base = declarative_base()`，得到两个互相独立的 Base 实例、两套 `metadata`。`MessageModel.session_id` 声明 `ForeignKey("sessions.id")` 时，由于 `sessions` 表注册在另一个 Base 的 metadata 上，SQLAlchemy 在映射 / create_all 阶段报 **mapper 冲突**，无法正确解析跨模型外键。

**解决**：统一到 `app/infrastructure/models/database/base.py` 中声明**唯一** `Base`，所有模型 `from .base import Base` 继承。任何新增模型（如未来的 `task` / `tool_log`）都必须复用该 `Base`，**严禁**在自己文件里再写 `declarative_base()`。

### SQLAlchemy 保留属性 metadata

**问题**：ORM 模型里曾写 `metadata = Column(JSON)` 作为扩展字段列名，运行时抛 `'metadata' is reserved when using the Declarative API` 错误——`metadata` 是 `declarative_base()` 上用于注册表结构的保留属性，不能作为列名。

**解决**：扩展字段列统一命名为 `meta`（`JSON` 类型，默认 `{}`）。新增列名时避开 SQLAlchemy / Declarative API 的保留字。

### 约定小结

- 所有 ORM 模型继承 `database/base.py` 的共享 `Base`
- 扩展字段一律叫 `meta`（JSON），不用保留字 `metadata`
- 时间字段用 `DateTime(timezone=True)` + UTC；`created_at` 用 `default`，`updated_at` 用 `onupdate`
- 对外导出走 `app/infrastructure/models/__init__.py` 与 `app/infrastructure/models/database/__init__.py`

---

## 当前状态与遗留

| 项目                 | 状态 | 说明                                                                 |
| -------------------- | ---- | -------------------------------------------------------------------- |
| `base.py`            | ✅   | 共享 `Base` 已落地，唯一 declarative_base 实例                       |
| `session.py`         | ✅   | `SessionModel` 已实现并被 `SessionManager` 使用                       |
| `messages.py`        | ✅   | `MessageModel` 已实现并被 `SessionManager` 使用                       |
| `task.py`            | ❌   | 预留任务表，待任务落库需求出现后实现                                   |
| `tool_log.py`        | ❌   | 预留工具调用日志表                                                     |
| `schemas/`           | ❌   | 预留 Pydantic 模型，待 API 层落地                                       |
| **DB 运行环境**      | 🔶   | `asyncpg` 驱动未安装，数据库恒降级（项目级遗留）；模型已就绪但未连真库验证 |

**下一步计划**：

1. 补 `asyncpg` 依赖并连通 PostgreSQL，验证 `SessionModel` / `MessageModel` 建表与增删查改
2. 依据 `task_doc/task.md` 落地 `task.py` 任务表
3. 依据 `integration_doc/tools_doc/tools.md` 落地 `tool_log.py` 工具调用日志表
4. API 层开发时填充 `schemas/` 请求 / 响应模型

---

## 常见问题

### Q: 为什么会话 id 用 UUID 字符串，消息 id 用自增 BigInteger？

两者定位不同：

- **会话 ID** 对外暴露（API 路径、Redis key），UUID 无法枚举、不易碰撞，且天然适合作为分布式缓存 key
- **消息 ID** 纯内部使用，量级大，自增 `BigInteger` 插入高效、索引紧凑；用户不直接引用消息 ID

### Q: `updated_at` 什么时候被刷新？

通过 `onupdate=datetime.now(UTC)`，在任何 UPDATE 语句执行时由 SQLAlchemy 自动刷新。软删除（`delete_session`）虽也是 UPDATE，但代码里已显式设置 `updated_at`，行为一致。

### Q: 为什么 `reasoning_content` 单独存一列，且读历史时不返回？

推理模型的思考过程（如 DeepSeek-R1 的 reasoning）若回灌给模型，会导致上下文膨胀、重复推理、成本上升。`SessionManager.get_messages()` 只取 `role in ("user", "assistant")` 的消息且不读该列，保证喂给 Agent 的历史干净。

### Q: 为什么用 `meta` JSON 而不是直接加列？

`meta` 用于**暂不定型**的扩展数据。早期列名曾用 `metadata` 触发 SQLAlchemy 保留字错误（已改名 `meta`）。若某字段长期稳定且需要查询过滤，应升级为正式列并加索引，而不是塞进 JSON。

### Q: 什么时候用软删除，什么时候用物理删除？

- **软删除（默认）**：`delete_session()` 把 `status` 置为 `deleted`，数据保留可恢复、可审计
- **物理删除**：`hard_delete_session()` 先删消息再删会话（FK 约束顺序），仅管理员 / 定时任务清理用

### Q: 新增一张表要注意什么？

1. 继承 `database/base.py` 的共享 `Base`，**不要**新写 `declarative_base()`
2. 列名避开保留字（`metadata` 等），扩展字段用 `meta`
3. 在 `app/infrastructure/models/database/__init__.py` 中导出（若对外使用还需在 `app/infrastructure/models/__init__.py` 导出）
4. 时间字段统一 UTC + `DateTime(timezone=True)`

---

## 相关文档

| 文档                                                 | 关联内容                                           |
| ----------------------------                         | --------------------------------------------       |
| [架构总览](../../architecture.md)                    | 数据模型层的分层定位与整体架构                     |
| [研发教训](../../lessons.md)                         | 项目级研发教训（metadata / declarative_base 出处） |
| [任务模块](../../application_doc/task_doc/task.md)   | `task.py` 预留对应的任务数据结构                   |
| [工具系统](../../integration_doc/tools_doc/tools.md) | `tool_log.py` 预留对应的工具抽象与内置工具         |
| [API 层](../../api_doc/api.md)                       | `schemas/` 预留对应的请求 / 响应模型               |
| [配置参考](../../config_doc/config.md)               | `DATABASE_URL` 等数据库连接配置                    |
| [部署文档](../../deployment.md)                      | 数据库部署与 `asyncpg` 依赖说明                    |
