# SessionManager 会话管理说明文档

> **更新日期**：2026-08-04
> **模块**：`app/services/session_manager.py`
> **文档定位**：SessionManager 独立说明 —— 会话生命周期管理、Redis 热缓存 + DB 持久化、分页 / 搜索 / 统计。

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

SessionManager 是整个多轮对话系统的**入口与基石**，位于服务层，负责会话与消息两条数据链路的完整生命周期：

1. **会话生命周期管理**：创建、查询、软删除 / 硬删除
2. **消息持久化**：存储 user / assistant 历史消息，支持分页读取
3. **缓存加速**：通过 Redis 热缓存减少数据库查询压力
4. **安全隔离**：会话与用户绑定，API 层据此做权限校验（`user_id` 校验）
5. **统计聚合**：消息数 / Token 总数 / 最后消息时间，优先取缓存

### 数据链路

```text
API 层（session / chat 路由）
        │
        ▼
SessionManager ──► Redis（热缓存：session:{id} / user_sessions:... / session_stats:...）
        │
        └──────► Database（SessionModel / MessageModel，SQLAlchemy async）
```

- 基础设施（Redis / DB）由 `AppState.initialize()` 统一创建后注入（见 [服务层总览](../service.md)）
- 构造依赖：`redis_client`（`redis.asyncio.Redis | None`）+ `db_session_factory`（`async_sessionmaker`）

### 设计原则

1. **双存储**：Redis 为热缓存（元数据 / 列表 / 统计），Database 为持久化（会话 + 消息）
2. **cache-through**：查询走「Redis → DB → 回写 Redis」，DB 未命中则不缓存空值
3. **软删除默认**：`delete_session` 采用软删除（`status="deleted"`），保留数据可回溯；物理删除仅限管理员 / 定时任务
4. **降级容忍**：Redis 不可用时打印 `[WARN]`，缓存路径退化为直接查 DB

---

## 核心类与方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `create_session` | `(user_id, system_prompt=None, title=None) -> dict` | 生成 UUID → DB 持久化 → 预热 Redis 缓存；默认 system_prompt「你是一个友好的AI助手」、title「新对话」 |
| `get_session` | `(session_id) -> dict \| None` | Redis → DB 缓存穿透保护，DB 命中回写 Redis |
| `get_messages` | `(session_id, limit=50, offset=0) -> list[dict]` | 仅返回 user / assistant 角色消息，按 `created_at` 升序，OFFSET 分页 |
| `add_message` | `(session_id, role, content, reasoning_content=None, token_count=0) -> int` | 插入消息记录，返回消息 ID（`inserted_primary_key[0]`） |
| `delete_session` | `(session_id) -> None` | 软删除：删 Redis 缓存 + DB 更新 `status="deleted"` |
| `hard_delete_session` | `(session_id) -> None` | 物理删除：先删消息（外键约束）再删会话，仅管理员 / 定时任务使用 |
| `list_sessions` | `(user_id, limit=20, offset=0, include_stats=True) -> list[dict]` | 活跃会话列表，第一页走 Redis 缓存（30s TTL） |
| `list_sessions_v2` | `(user_id, limit=20, offset=0, status="active", keyword=None, start_date=None, end_date=None, sort_by="updated_at", sort_order="desc", include_stats=True) -> tuple[list, int]` | 增强版：搜索 / 筛选 / 排序 + 总数统计 |
| `_get_session_stats` | `(session_id, db) -> dict` | 聚合查询消息数 / Token 数 / 最后消息时间，60s 缓存 |

---

## 关键实现详解

### Redis 缓存键设计

| 缓存键 | TTL | 内容 | 失效策略 |
| --- | --- | --- | --- |
| `session:{session_id}` | 7 天 | 会话元数据（id / user_id / system_prompt / created_at / message_count / total_tokens） | 删除会话时主动清除 |
| `user_sessions:{user_id}:page:{n}` | 30 秒 | 会话列表第一页（`n = offset // limit`） | TTL 短，列表频繁变化 |
| `session_stats:{session_id}` | 60 秒 | 聚合统计（message_count / total_tokens / last_message_at） | TTL 短 |

> **注意**：`session_ttl = 3600 * 24 * 7` 在 `__init__` 中**硬编码**（7 天）。配置项 `redis_session_ttl`（默认 `604800`）虽然存在，但当前代码未引用它——两者数值恰好相等（604800 = 7 × 24 × 3600）。

### `get_session`：缓存穿透保护

```text
get_session(session_id)
  1. Redis 查 session:{id} → 命中返回
  2. 未命中 → 查 DB（SELECT SessionModel WHERE id = session_id）
     · 不存在 → return None
     · 存在 → 回写 Redis（message_count / total_tokens 置 0，懒加载）→ 返回
```

- DB 未命中**不缓存空值**，存在缓存穿透攻击面（文件内注释了「布隆过滤器 + 空值缓存」增强方案，作为演进参考，见下）
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

### 降级策略与已知局限

- **Redis 降级**：构造时 `redis_client is None` 仅打印 `[WARN] SessionManager: Redis 不可用，缓存降级`；**方法内部未对 None 做兜底**——若传入 `None`，首次缓存读写（如 `create_session` 的 `self.redis.set`）会抛 `AttributeError`。基础设施降级由 `AppState` 保证 Redis 可用性，当前实现并未真正打通「无 Redis 运行」路径
- **遗留注释**：文件内保留两份被注释的增强方案作为演进参考
  - `get_session` 增强版：布隆过滤器快速过滤 + 空值缓存（60s）防穿透攻击
  - `get_messages_cursor`：游标分页（基于 ID 的 `cursor`），替代大规模数据下的 OFFSET 分页

### 边缘情况

- `add_message` 返回 `inserted_primary_key[0]`，若驱动不支持主键回读则回退 `0`
- 消息查询只返回 `role` / `content` 两个字段，`reasoning_content` / `token_count` 不随历史消息返回
- `list_sessions` / `list_sessions_v2` 的 `include_stats=True` 会为每个会话触发一次统计查询（或缓存命中），列表较长时注意 N+1 压力

---

## 使用示例

```python
# 创建会话
session = await app_state.session_manager.create_session(
    user_id="user-123",
    system_prompt="你是良率分析助手",
    title="RCA 分析",
)
session_id = session["id"]

# 查询（Redis → DB 缓存穿透保护）
sess = await app_state.session_manager.get_session(session_id)

# 存取消息
await app_state.session_manager.add_message(
    session_id,
    "user",
    "分析这批不良率",
    token_count=42,
)
messages = await app_state.session_manager.get_messages(session_id, limit=20)

# 会话列表（第一页走 Redis 缓存）
sessions = await app_state.session_manager.list_sessions(user_id="user-123")

# 增强查询：按标题关键词搜索 + 总数分页
sessions, total = await app_state.session_manager.list_sessions_v2(
    user_id="user-123",
    keyword="RCA",
    status="active",
    sort_by="updated_at",
    sort_order="desc",
)

# 软删除 / 硬删除（后者仅管理员 / 定时任务）
await app_state.session_manager.delete_session(session_id)
# await app_state.session_manager.hard_delete_session(session_id)
```

> 实际调用方为 `app/api/routes/session.py`（会话 CRUD）与 `app/api/routes/chat.py`（存消息），经 `get_session_manager` 依赖注入获取单例实例。

---

## 配置关联

相关配置集中在 `app/config/settings.py`（详见 [config 文档](../../config_doc/config.md)）：

| 配置项 | 默认值 | 当前是否被引用 | 说明 |
| --- | --- | --- | --- |
| `redis_url` | `redis://localhost:6379/0` | ✅ | Redis 连接地址（`AppState` 读取） |
| `redis_session_ttl` | `604800`（7 天） | ❌ | 会话缓存 TTL —— 代码中硬编码 `3600 * 24 * 7`，未读取此配置 |
| `database_url` | `postgresql+asyncpg://...` | ✅ | DB 连接地址（`AppState` 读取） |
| `database_pool_size` / `database_max_overflow` | `20` / `10` | ✅ | DB 连接池（`AppState` 读取） |

> SessionManager 本身**不直接读取任何配置**：Redis / DB 连接均由 `AppState.initialize()` 创建后注入，本模块只负责缓存键与 TTL 的定义。

---

## 相关文档

- [服务层总览](../service.md)（SessionManager 在服务层的定位）
- [ContextManager 上下文管理](../context_doc/context.md)（下游依赖方：经 `get_session` / `get_messages` 组装上下文）
- [TaskService 任务调度](../task_doc/task.md)
- [API 模块](../../api_doc/api.md)（`session.py` / `chat.py` 路由，本模块的上游调用方）
- [核心层说明](../../core_doc/core.md)
- [架构设计](../../architecture.md)
- [配置说明](../../config_doc/config.md)
- [HANDOFF](../../HANDOFF.md)
