# 会话持久化 Store

DB-F05a 已实现。源码：[领域端口](../../../app/domain/ports/session_store.py)、[PostgreSQL 适配器](../../../app/infrastructure/session_store.py)。业务参数与缓存策略由 [SessionManager](../../application_doc/session_doc/session.md) 维护。

## 职责与依赖

`SessionStorePort` 只依赖标准库与 shared，传递标识、dict、列表及分页总数，不暴露 ORM、Session 或事务。`PostgresSessionStore` 使用现有 ORM 和注入的 Session 工厂；Container 负责注入，Application 不导入 SQLAlchemy。没有通用 Repository、重试或第二迁移机制。

当前 Container 将原工厂包装为 Store，尚未接入 DatabaseRuntime。完整 schema/readiness、首次失败消费者装配及 HTTP 503 属 F05b；消费者排空属 F05c。不得据此宣称当前应用已具备完整数据库生命周期保证。

## 接口与数据契约

| 方法 | 行为 |
| --- | --- |
| `ensure_available` | 同步读取外部准入状态、工厂及 Store 清理失败封锁，不执行探针 |
| `create_session` / `get_session` | 普通会话 dict；创建返回实际写入的创建时间；读取不存在为 None |
| `add_message` | 提交后返回正整数消息 ID |
| `get_messages` | 仅 user/assistant，created_at/id 倒序截取最新窗口，再恢复正序；before_message_id 排他 |
| `delete_session` | 状态改为 deleted，消息保留 |
| `hard_delete_session` | 同一事务先删消息、再删会话 |
| `list_sessions` | active 会话，updated_at 倒序且 NULL 最后；不附统计 |
| `get_session_stats` | user/assistant 的数量、token 总数与最后消息时间 |
| `list_sessions_v2` | 返回列表与过滤后总数；状态、关键词、时间及排序保持原管理器语义；统计由应用补充 |

分页归一、默认标题/提示词、UUID、缓存键与 TTL 由应用负责。缓存命中前检查准入，Redis 等待期间状态变化也不得返回旧缓存。单次连接失败是操作事实，并不永久判定所有连接不可用；全局失效/探测恢复由后续 runtime 装配负责。

## 事务、期限与资源责任

每次调用创建独立 worker，只有该 worker 使用自己的 Session，执行 SQL、一次 commit 及 finally rollback/close。读取同样结束事务并释放连接后返回；Redis IO 不持有数据库连接。硬删两条 DELETE 原子提交，不自动重放写入。

operation 总预算包含清理，业务预留不超过 cleanup 配置及总预算一半的窗口；每次 SQL/commit 前后检查期限。清理受自身上限和总剩余预算共同限制。外部取消原样传播，不开启后续业务动作，必要回滚仍允许执行。

调用方有界等待，未结束 worker 保留强引用与 pending 登记，异常被接收；清理不完整封锁新准入，不能把调用方返回当资源释放。接入受控 runtime 工厂后，底层未关闭 Session 仍由 runtime 登记；当前普通工厂装配尚不具备该最终回收保证，留 F05b/F05c 完成，不另建第二资源 Owner。

## 错误与提交事实

已知连接、超时、权限和结构故障转换为脱敏 `PersistenceUnavailableError`；约束冲突、未知 SQL/程序错误保持原类型。异常公共契约见[错误说明](../../shared_doc/error_handling.md#持久化不可用)。清理不得覆盖已发生的主失败。

`committed=True` 表示已收到提交成功；`commit_unknown=True` 表示提交已开始但未收到确认。迟到提交成功、清理失败与取消均保留已知事实；无成功响应不能推出未写入，调用方不能盲目重试。

## 验证与维护

[应用单测](../../../tests/unit/test_session_manager.py) 验证参数、缓存与不可用边界；[Store 单测](../../../tests/unit/test_session_store.py) 验证取消、期限、迟到清理与提交事实；[真实 PostgreSQL 测试](../../../tests/integration/test_session_store.py) 验证 CRUD、消息窗口、过滤统计、硬删原子性、提交响应丢失、慢 SQL 及 Redis IO 前归还连接。

测试环境和命令见[部署说明](../../project/deployment.md#postgresql-隔离测试环境)。设计依据及当前交付边界见 [DB-ADR-001](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md)，本片实测记录见[计划评审](../../history/completed-work.md#db-f05a-review)。
