# SessionManager 会话管理说明文档

> **更新日期**：2026-08-29
> **模块**：`app/application/session/session_manager.py`
> **文档定位**：SessionManager 独立说明 —— 会话生命周期管理、消息持久化、Redis 热缓存 + DB 持久化、分页 / 搜索 / 统计。

---

## 📋 目录

- [模块概述](#模块概述)
- [核心类与方法](#核心类与方法)
- [关键实现详解](#关键实现详解)
- [使用示例](#使用示例)
- [配置关联](#配置关联)
- [相关文档](#相关文档)

---

## 模块概述

### 定位与职责

SessionManager 是多轮对话系统的**入口与基石**，负责会话与消息两条数据链路的完整生命周期：

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
        └──────► Database（SessionModel / MessageModel，SQLAlchemy async）
```

- 构造依赖 `redis_client`（`redis.asyncio.Redis | None`）与 `db_session_factory`（`async_sessionmaker`），均由 `Container.initialize()` 创建后注入，本模块不直接读取配置
- 上游调用方：`app/api/routes/session.py`（会话 CRUD）与 `app/api/routes/chat.py`（存消息）

### 构造参数

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `redis_client` | 必填 | `Container` 注入 | `redis.asyncio.Redis \| None`，热缓存；为 None 时缓存降级直查 DB |
| `db_session_factory` | 必填 | `Container` 注入 | `async_sessionmaker`，DB 会话工厂 |

> `session_ttl = 3600 * 24 * 7`（7 天）在 `__init__` 中硬编码，配置项 `redis_session_ttl` 未引用（数值恰好相等）。

---

## 核心类与方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `create_session` | `(user_id, system_prompt=None, title=None) -> dict` | 创建会话：UUID + DB 持久化 + 预热 Redis，默认 system_prompt「你是一个友好的AI助手」/ title「新对话」 |
| `get_session` | `(session_id) -> dict \| None` | Redis → DB 缓存穿透保护，DB 命中回写 Redis |
| `get_messages` | `(session_id, limit=50, offset=0) -> list[dict]` | 历史消息（仅 user / assistant），`created_at` 升序，OFFSET 分页 |
| `add_message` | `(session_id, role, content, reasoning_content=None, token_count=0) -> int` | 写入消息记录，返回消息 ID（`inserted_primary_key[0]`） |
| `delete_session` | `(session_id) -> None` | 软删除：删 Redis 键 + DB 更新 `status="deleted"` |
| `hard_delete_session` | `(session_id) -> None` | 物理删除：先删消息（外键约束）再删会话，仅管理员 / 定时任务 |
| `list_sessions` | `(user_id, limit=20, offset=0, include_stats=True) -> list[dict]` | 活跃会话列表，第一页走 Redis 缓存 |
| `list_sessions_v2` | `(user_id, limit=20, offset=0, status="active", keyword=None, start_date=None, end_date=None, sort_by="updated_at", sort_order="desc", include_stats=True) -> tuple[list, int]` | 增强查询：搜索 / 筛选 / 排序 + 总数统计 |

> `session_id` 为 `SessionId`、`user_id` 为 `UserId`（`app/shared/types.py` NewType）。

---

## 关键实现详解

### Redis 缓存键设计

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
  1. Redis 查 session:{id} → 命中返回
  2. 未命中 → 查 DB（SELECT SessionModel WHERE id = session_id）
     · 不存在 → return None
     · 存在 → 回写 Redis（message_count / total_tokens 置 0，懒加载）→ 返回
```

- DB 未命中**不缓存空值**，存在缓存穿透攻击面（文件内注释「布隆过滤器 + 空值缓存」增强方案作为演进参考）
- 回写时 `message_count` / `total_tokens` 固定为 0，实际统计走 `_get_session_stats` 懒加载

### `list_sessions`：第一页缓存策略

- 参数防护：`limit` 收敛到 `[1, 100]`，`offset` 下限为 0
- 仅当 `offset == 0` 时尝试读 / 写缓存（热点第一页），后续页直接查 DB
- 查询条件：`user_id` + `status == "active"`，按 `updated_at.desc().nullslast()` 排序
- `include_stats=True` 时逐会话调用 `_get_session_stats`

### 统计聚合 `_get_session_stats`

```sql
SELECT count(id) AS message_count,
       coalesce(sum(token_count), 0) AS total_tokens,
       max(created_at) AS last_message_at
FROM messages
WHERE session_id = ? AND role IN ('user', 'assistant')
```

- 先查 `session_stats:{id}` 缓存，未命中再聚合，结果写缓存（60s）
- 只统计 user / assistant 角色（排除 system / reasoning）

### 软删除 vs 硬删除

| 操作 | 实现 | 适用场景 |
| --- | --- | --- |
| `delete_session`（软） | 删 Redis 键 + `UPDATE sessions SET status='deleted', updated_at=now` | 常规删除（推荐），可回溯 |
| `hard_delete_session`（硬） | 删 Redis 键 + `DELETE FROM messages`（先子表，外键约束）+ `DELETE FROM sessions` | 管理员 / 定时清理 |

### `list_sessions_v2`：增强查询

- **状态筛选**：`status="active" / "archived" / "deleted"`；传 `None` 查全部（排除 `deleted`）
- **关键词**：`SessionModel.title.ilike(f"%{keyword}%")`
- **日期范围**：`created_at >= start_date` / `<= end_date`
- **排序**：`sort_by` 白名单 `created_at / updated_at / title`，`desc` 用 `nullslast`、`asc` 用 `nullsfirst`
- **总数**：先 `SELECT count(id)` 再分页，返回 `(session_list, total_count)`

### 边缘情况

| 场景 | 行为 |
| --- | --- |
| Redis 不可用（`redis_client=None`） | 缓存读写经 `_cache_*` 判空辅助降级，直查 DB |
| `limit` 越界 | 收敛到 `[1, 100]` |
| `offset` 为负 | 归零 |
| `add_message` 无主键回读 | 返回 `0` |
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

相关配置集中在 `app/config/settings.py`（详见 [config 文档](../../config_doc/config.md)）：

| 配置项 | 默认值 | 当前是否被引用 | 说明 |
| --- | --- | --- | --- |
| `redis_url` | `redis://localhost:6379/0` | ✅ | Redis 连接地址（`Container` 读取） |
| `redis_session_ttl` | `604800`（7 天） | ❌ | 会话缓存 TTL —— 代码中硬编码 `3600 * 24 * 7`，未读取此配置 |
| `database_url` | `postgresql+asyncpg://...` | ✅ | DB 连接地址（`Container` 读取） |
| `database_pool_size` / `database_max_overflow` | `20` / `10` | ✅ | DB 连接池（`Container` 读取） |

> SessionManager 本身**不直接读取任何配置**：Redis / DB 连接均由 `Container.initialize()` 创建后注入，本模块只负责缓存键与 TTL 的定义。

---

## 相关文档

- [应用层说明](../README.md)（SessionManager 的定位）
- [ContextManager 上下文管理](../context_doc/context.md)（下游依赖方：经 `get_session` / `get_messages` 组装上下文）
- [路由模块](../../api_doc/routes_doc/routes.md)（`session.py` / `chat.py` 路由，本模块上游调用方）
- [配置说明](../../config_doc/config.md)
- [架构设计](../../architecture.md)
