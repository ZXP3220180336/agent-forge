# SessionManager 会话管理说明文档

> **更新日期**：2026-09-25
> **模块**：`app/application/session/session_manager.py`
> **文档定位**：SessionManager 独立说明 —— 会话生命周期管理、消息持久化、Redis 热缓存 + DB 持久化、分页 / 搜索 / 统计。

---

## 📋 目录

- [模块概述](#模块概述)
- [核心类与方法](#核心类与方法)
- [关键实现详解](#关键实现详解)
- [使用示例](#使用示例)
- [配置关联](#配置关联)
- [验证入口](#验证入口)
- [相关文档](#相关文档)

---

## 模块概述

### 定位与职责

SessionManager 负责会话与消息用例的参数、返回数据和缓存编排，数据库操作委托持久化端口：

1. **会话生命周期**：创建、查询、软删除 / 硬删除
2. **消息持久化**：存储 user / assistant 历史消息，支持分页读取
3. **缓存加速**：Redis 热缓存（cache-through），减少数据库查询压力
4. **统计聚合**：消息数 / Token 总数 / 最后消息时间，优先取缓存

### 依赖关系

```text
API 层（session / chat 路由）
        │
        ▼
SessionManager ──► Redis（热缓存：session:{id} / user_sessions:... / session_stats:...）
        │
        └──────► SessionStorePort（普通 dict / list / int / None）
                         ▲
                         │ 实现
                 SQLAlchemySessionStore → DatabaseRuntime
```

- 构造依赖 `redis_client`（`redis.asyncio.Redis | None`）与 `store`（`SessionStorePort | None`），由装配根注入；本模块不直接读取配置，不导入 SQLAlchemy、ORM 或数据库驱动。
- Store 只交换普通数据，不暴露会话、事务或 ORM。每次调用结束后释放数据库资源，再由 Manager 访问缓存。适配器的 SQL、事务及错误分类见 [SessionStore](../../infrastructure_doc/database_doc/session_store.md)。
- runtime 就绪装配和 API 503 接线属于 DB-F05b，实施与验证状态统一见 [ALIGNMENT](../../ALIGNMENT.md)。
- 上游调用方：`app/api/routes/session.py`（会话 CRUD）与 `ChatService`（聊天预检、消息与结果提交）

### 构造参数

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `redis_client` | 必填 | `Container` 注入 | `redis.asyncio.Redis \| None`，热缓存；为 None 时缓存降级直查 DB |
| `store` | 必填 | 装配根注入 | `SessionStorePort \| None`；为 None 时允许构造，操作时抛 `PersistenceUnavailableError` |

> `session_ttl = 3600 * 24 * 7`（7 天）在 `__init__` 中硬编码，配置项 `redis_session_ttl` 未引用（数值恰好相等）。

---

## 核心类与方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `create_session` | `(user_id, system_prompt=None, title=None) -> dict` | 创建会话：UUID + DB 持久化 + 预热 Redis，默认 system_prompt「你是一个友好的AI助手」/ title「新对话」 |
| `get_session` | `(session_id) -> dict \| None` | Redis → DB 缓存穿透保护，DB 命中回写 Redis |
| `get_messages` | `(session_id, limit=50, offset=0, before_message_id=None) -> list[dict]` | 取最新窗口并按时间正序返回历史消息；可用消息 ID 建立排他的运行快照上界 |
| `add_message` | `(session_id, role, content, reasoning_content=None, token_count=0) -> int` | 写入并返回正消息 ID；数据库未返回有效主键时失败 |
| `delete_session` | `(session_id) -> None` | 软删除：删 Redis 键 + DB 更新 `status="deleted"` |
| `hard_delete_session` | `(session_id) -> None` | 物理删除：先删消息（外键约束）再删会话，仅管理员 / 定时任务 |
| `list_sessions` | `(user_id, limit=20, offset=0, include_stats=True) -> list[dict]` | 活跃会话列表，第一页走 Redis 缓存 |
| `list_sessions_v2` | `(user_id, limit=20, offset=0, status="active", keyword=None, start_date=None, end_date=None, sort_by="updated_at", sort_order="desc", include_stats=True) -> tuple[list, int]` | 增强查询：搜索 / 筛选 / 排序 + 总数统计 |

> `session_id` 为 `SessionId`、`user_id` 为 `UserId`（`app/shared/types.py` NewType）。

---

## 关键实现详解

### Redis 缓存键设计

所有公开操作先经 `_available_store()` 同步检查 `store.ensure_available()`，再访问缓存或调用 Store。已知持久化不可用时，即使缓存命中也抛 `PersistenceUnavailableError`；异常码为 `PERSISTENCE_UNAVAILABLE`，不返回空列表或伪造成功。

单会话、列表和统计缓存命中后，在返回前再次同步检查可用性，防止等待 Redis 期间数据库状态已变为不可用却仍返回缓存成功。

`create_session` 在 Manager 生成 UUID 并应用提示词、标题默认值。`created_at` 来自 Store 实际写入并返回的数据，不另取应用时钟。写入失败或取消时不预热缓存，也不自动重试。

| 缓存键 | TTL | 内容 | 失效策略 |
| --- | --- | --- | --- |
| `session:{session_id}` | 7 天 | 会话元数据（id / user_id / system_prompt / created_at / message_count / total_tokens） | 删除会话时主动清除 |
| `user_sessions:{user_id}:page:{n}` | 30 秒 | 会话列表第一页（`n = offset // limit`） | TTL 短，列表频繁变化 |
| `session_stats:{session_id}` | 60 秒 | 聚合统计（message_count / total_tokens / last_message_at） | TTL 短 |

### Redis 缓存辅助（None 降级）

所有缓存读写统一经三个判空辅助方法，消除 `self.redis: Redis | None` 的 Optional 访问（`redis` 为 None 时整体降级直查 DB，业务方法无需逐处判空）：

| 方法 | 行为（redis 为 None 时） |
| --- | --- |
| `_cache_get(key) -> bytes \| str \| None` | 返回 `None`（跳过缓存，直查 DB） |
| `_cache_set(key, value, ex=None) -> None` | 直接跳过，不崩溃 |
| `_cache_delete(key) -> None` | 直接跳过，不崩溃 |

### `get_session`：缓存穿透保护

```text
get_session(session_id)
  1. 检查 Store 可用性
  2. Redis 查 session:{id} → 命中返回
  3. 未命中 → await store.get_session(session_id)
     · 不存在 → return None
     · 存在 → 回写 Redis（message_count / total_tokens 置 0，懒加载）→ 返回
```

- DB 未命中**不缓存空值**；本次未增加布隆过滤器或空值缓存。
- 回写时 `message_count` / `total_tokens` 固定为 0，实际统计走 `_get_session_stats` 懒加载

### `get_messages`：最近历史窗口

`get_messages` 的 `limit` 表示“从最新消息向前取多少条”，而不是从会话首条消息开始
截取。Manager 将分页和快照参数传给 Store，由其选择最新窗口并恢复时间正序，供历史接口和
`ContextManager` 按对话发生顺序消费。SQL 排序及边界实现见 [SessionStore](../../infrastructure_doc/database_doc/session_store.md)。

- `before_message_id` 是排他的快照上界，先过滤 `id < before_message_id`，再从该快照中取
  最近窗口。
- `offset` 从该最新窗口起点计算，向更早的历史分页；它不改变返回结果的时间正序。
- 这条路径只返回 `user` / `assistant` 消息；`system`、`reasoning` 和 token 计数仍不随历史
 结果返回。

### `list_sessions`：第一页缓存策略

- 参数防护：`limit` 收敛到 `[1, 100]`，`offset` 下限为 0
- 仅当 `offset == 0` 时尝试读 / 写缓存（热点第一页），后续页直接查 DB
- Store 返回指定用户的活跃会话，按更新时间降序排列，空时间排在最后。
- `include_stats=True` 时逐会话调用 `_get_session_stats`
- 沿用现有缓存键：键中不区分 `limit` 或 `include_stats`，本次未改变该策略；创建和消息追加也不新增列表或统计键的主动失效。

### 统计聚合 `_get_session_stats`

- 签名为 `_get_session_stats(session_id) -> dict`，不接收数据库会话。
- 先检查 Store 可用性，再查 `session_stats:{id}` 缓存；未命中调用 `store.get_session_stats(session_id)`，返回后写缓存（60s）。
- 只统计 user / assistant 角色（排除 system / reasoning）

### 软删除 vs 硬删除

| 操作 | 实现 | 适用场景 |
| --- | --- | --- |
| `delete_session`（软） | 检查可用性 → 删单会话 Redis 键 → Store 更新状态 | 常规删除，可回溯 |
| `hard_delete_session`（硬） | 检查可用性 → 删单会话 Redis 键 → Store 在同一事务内先删消息再删会话 | 管理员 / 定时清理 |

### `list_sessions_v2`：增强查询

- **状态筛选**：`status="active" / "archived" / "deleted"`；传 `None` 查全部（排除 `deleted`）
- **关键词**：标题不区分大小写匹配，参数原样委托 Store。
- **日期范围**：`created_at >= start_date` / `<= end_date`
- **排序**：支持 `created_at / updated_at / title`；具体排序及回退由 Store 承担。
- **总数**：Store 返回无统计的分页与匹配总数；Manager 根据 `include_stats` 合并统计，返回 `(session_list, total_count)`，不缓存整页 v2 结果。

### 边缘情况

| 场景 | 行为 |
| --- | --- |
| Redis 不可用（`redis_client=None`） | 缓存读写经 `_cache_*` 判空辅助降级，直查 DB |
| Store 缺失或已知不可用 | 在缓存访问前抛 `PersistenceUnavailableError`，不借缓存掩盖不可用 |
| `limit` 越界 | 收敛到 `[1, 100]` |
| `offset` 为负 | 归零 |
| `add_message` 无有效主键回读 | 由 Store 拒绝并传播失败，调用方不得继续构建无边界上下文 |
| Store 取消或提交未确认 | 传播取消/明确错误；不自动重试，未确认创建不写缓存 |
| `get_session` DB 未命中 | 返回 `None`（不缓存空值，存在穿透攻击面） |
| 消息查询 | 只返回 `role` / `content` 两字段，`reasoning_content` / `token_count` 不随历史返回 |
| 统计开启 | 逐会话一次聚合查询（或缓存命中），列表较长时注意 N+1 压力 |

---

## 使用示例

```python
# 创建会话
session = await container.session_manager.create_session(
    user_id="user-123",
    system_prompt="你是良率分析助手",
    title="RCA 分析",
)
session_id = session["id"]

# 查询（Redis → DB 缓存穿透保护）
sess = await container.session_manager.get_session(session_id)

# 存取消息
await container.session_manager.add_message(
    session_id, "user", "分析这批不良率", token_count=42,
)
messages = await container.session_manager.get_messages(session_id, limit=20)

# 增强查询：按标题关键词搜索 + 总数分页
sessions, total = await container.session_manager.list_sessions_v2(
    user_id="user-123", keyword="RCA", status="active",
)
```

---

## 配置关联

连接、池与超时配置集中在 [config 文档](../../config_doc/config.md)。SessionManager 不读取配置，只接收 Redis 和 Store；数据库资源由 runtime 与适配器管理。会话缓存 TTL 仍使用 `session_ttl` 常量，未接入 `redis_session_ttl`。DB-F05b 的装配就绪检查及 HTTP 错误映射状态见 [ALIGNMENT](../../ALIGNMENT.md)。

---

## 验证入口

`tests/unit/test_session_manager.py` 使用端口假对象验证可用性检查早于缓存、创建默认值与 UUID、缓存键/TTL、第一页策略、消息窗口参数、统计合并以及取消/提交未知不重试。SQL 与真实事务验证由 [SessionStore](../../infrastructure_doc/database_doc/session_store.md) 的测试承担；验证状态见 [ALIGNMENT](../../ALIGNMENT.md)。

## 相关文档

- [应用层说明](../README.md)（SessionManager 的定位）
- [ContextManager 上下文管理](../context_doc/context.md)（下游依赖方：经 `get_session` / `get_messages` 组装上下文）
- [ChatService](../chat_doc/chat.md)（聊天用例调用方）与[路由模块](../../api_doc/routes_doc/routes.md)（会话 CRUD）
- [配置说明](../../config_doc/config.md)
- [SessionStore 持久化适配器](../../infrastructure_doc/database_doc/session_store.md)（SQL、事务、资源和错误边界）
- [架构设计](../../project/architecture.md)
