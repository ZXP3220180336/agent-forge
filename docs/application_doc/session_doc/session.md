# SessionManager 会话管理说明文档

> **对应代码**：`app/application/session/session_manager.py`
> **更新日期**：2026-08-29
> **职责**：会话生命周期管理、消息持久化、Redis 热缓存 + DB 持久化、分页/搜索/统计
> **状态**：✅ 已实现
> **配套**：Redis / DB 连接由 `Container.initialize()` 创建后注入，本模块不直接读取配置

---

## 📋 目录

- [SessionManager 会话管理说明文档](#sessionmanager-会话管理说明文档)
  - [📋 目录](#-目录)
  - [定位与职责](#定位与职责)
  - [接口契约](#接口契约)
  - [行为边界](#行为边界)
  - [使用示例](#使用示例)
  - [设计决策](#设计决策)
  - [测试](#测试)
  - [相关文档](#相关文档)

---

## 定位与职责

SessionManager 是多轮对话系统的**入口与基石**，负责会话与消息两条数据链路的完整生命周期：

1. **会话生命周期**：创建、查询、软删除 / 硬删除
2. **消息持久化**：存储 user / assistant 历史消息，支持分页读取
3. **缓存加速**：Redis 热缓存（cache-through），减少数据库查询压力
4. **统计聚合**：消息数 / Token 总数 / 最后消息时间，优先取缓存

构造依赖：`redis_client`（`redis.asyncio.Redis | None`）+ `db_session_factory`（`async_sessionmaker`），均由 `Container` 注入。上游调用方：`app/api/routes/session.py`（会话 CRUD）与 `app/api/routes/chat.py`（存消息）。

## 接口契约

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `create_session(user_id, system_prompt=None, title=None) -> dict` | 异步 | 创建会话（UUID + DB 持久化 + 预热 Redis），默认 system_prompt「你是一个友好的AI助手」/ title「新对话」 |
| `get_session(session_id) -> dict \| None` | 异步 | Redis → DB 缓存穿透保护，DB 命中回写 Redis |
| `get_messages(session_id, limit=50, offset=0) -> list[dict]` | 异步 | 历史消息（仅 user / assistant），`created_at` 升序，OFFSET 分页 |
| `add_message(session_id, role, content, reasoning_content=None, token_count=0) -> int` | 异步 | 写入消息记录，返回消息 ID |
| `delete_session(session_id) -> None` | 异步 | 软删除（删 Redis 键 + DB 更新 `status="deleted"`） |
| `hard_delete_session(session_id) -> None` | 异步 | 物理删除（先删消息再删会话），仅管理员 / 定时任务 |
| `list_sessions(user_id, limit=20, offset=0, include_stats=True) -> list[dict]` | 异步 | 活跃会话列表，第一页走 Redis 缓存 |
| `list_sessions_v2(user_id, limit=20, offset=0, status="active", keyword=None, start_date=None, end_date=None, sort_by="updated_at", sort_order="desc", include_stats=True) -> tuple[list, int]` | 异步 | 增强查询：搜索 / 筛选 / 排序 + 总数统计 |

> `session_id` 为 `SessionId`、`user_id` 为 `UserId`（`app/shared/types.py` NewType）。

## 行为边界

| 场景 | 行为 |
| --- | --- |
| Redis 不可用（`redis_client=None`） | 缓存读写经 `_cache_get` / `_cache_set` / `_cache_delete` 判空辅助：读取返回 `None`、写入直接跳过，退化为直查 DB |
| `limit` 越界 | 收敛到 `[1, 100]` |
| `offset` 为负 | 归零 |
| `add_message` 无主键回读 | 返回 `0` |
| `get_session` DB 未命中 | 返回 `None`（不缓存空值，存在缓存穿透攻击面，增强方案见设计决策） |
| 消息查询 | 只返回 `role` / `content` 两字段，`reasoning_content` / `token_count` 不随历史返回 |
| 统计开启 | 逐会话一次聚合查询（或缓存命中），列表较长时注意 N+1 压力 |

## 使用示例

```python
# 创建会话并查询（Redis → DB 缓存穿透保护）
session = await container.session_manager.create_session(
    user_id="user-123",
    system_prompt="你是良率分析助手",
    title="RCA 分析",
)
sess = await container.session_manager.get_session(session["id"])

# 存消息 + 分页读取
await container.session_manager.add_message(
    session["id"], "user", "分析这批不良率", token_count=42,
)
messages = await container.session_manager.get_messages(session["id"], limit=20)

# 增强查询：按标题关键词搜索 + 总数分页
sessions, total = await container.session_manager.list_sessions_v2(
    user_id="user-123", keyword="RCA", status="active",
)
```

## 设计决策

- **cache-through 读路径**：`get_session` 走「Redis → DB → 回写 Redis」；DB 未命中不缓存空值。文件内遗留注释记录「布隆过滤器 + 空值缓存」增强方案作为演进参考
- **软删除默认**：`delete_session` 软删除保留数据可回溯；物理删除仅限特殊场景
- **session TTL 硬编码**：`session_ttl = 3600 * 24 * 7` 在 `__init__` 硬编码，配置项 `redis_session_ttl`（默认 `604800`）存在但当前未引用（数值恰好相等）
- **增强查询**：`list_sessions_v2` 状态白名单（active / archived / deleted / 全量排除 deleted）+ 标题 `ilike` + 日期范围 + 排序（`desc` 用 `nullslast`、`asc` 用 `nullsfirst`）
- **Redis 缓存键**：`session:{id}`（7 天 TTL，元数据）/ `user_sessions:{user_id}:page:{n}`（30 秒 TTL，列表第一页）/ `session_stats:{id}`（60 秒 TTL，聚合统计）

## 测试

- `tests/unit/test_session_manager.py`：会话 CRUD / 缓存穿透 / 分页 / 统计的行为契约

## 相关文档

- [应用层说明](../README.md)（SessionManager 的定位）
- [ContextManager 上下文管理](../context_doc/context.md)（下游依赖方：经 `get_session` / `get_messages` 组装上下文）
- [API 模块](../../api_doc/api.md)（`session.py` / `chat.py` 路由，本模块上游调用方）
- [配置说明](../../config_doc/config.md)
- [架构设计](../../architecture.md)
