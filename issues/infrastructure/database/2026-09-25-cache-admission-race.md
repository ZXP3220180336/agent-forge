# DB-020：缓存等待期间准入状态变化被遗漏

日期：2026-09-25；状态：已修复；优先级：P2；来源：DB-F05a 开发阶段独立审查，未声称已发生生产事故。

## 现象与根因

SessionManager 三处缓存命中在 await Redis.get 后直接返回；等待期间数据库转为已知不可用仍返回缓存。

## 复现与修复

先补 get_session/list_sessions/_get_session_stats 三个参数化测试，Redis.get 内改变 Store 准入状态；旧实现 3 failed（DID NOT RAISE）。

保留入口检查，在缓存命中返回前再次同步 ensure_available，无额外数据库 IO。修复后管理器 39 passed。

## 取舍与验证

不增加重试、探针或新资源 Owner。应用参数、缓存键和 TTL 保持原样。工业依据为 [Python asyncio 协作调度](https://docs.python.org/3/library/asyncio-task.html) 与 [SQLAlchemy Session 事务生命周期](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)；本地红绿测试验证项目边界。

教训：异步等待可使先前准入判断失效；返回缓存同样需要核对当前准入。全量与真实数据库结果见 [F05a 评审](../../../docs/history/completed-work.md#db-f05a-review)，契约见[会话 Store](../../../docs/infrastructure_doc/database_doc/session_store.md)。
