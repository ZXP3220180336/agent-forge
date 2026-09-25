# DB-021：清理错误分类掩盖未知异常及提交事实

日期：2026-09-25；状态：已修复；优先级：P2；来源：DB-F05a 开发阶段独立审查，未声称已发生生产事故。

## 现象与根因

F05a 开发版清理分支把全部 DBAPIError 归为设施不可用，未知 ValueError 原抛时未保留已提交事实。

## 复现与修复

新增 ProgrammingError/ValueError 的提交后 close 失败复现，修复前 2 failed、19 passed。

操作与清理复用 _unavailable_reason 白名单；未知异常原样传播并附 committed/commit_unknown。修复后 Store 21 passed。

## 取舍与验证

不增加重试、探针或新资源 Owner。应用参数、缓存键和 TTL 保持原样。工业依据为 [Python asyncio 协作调度](https://docs.python.org/3/library/asyncio-task.html) 与 [SQLAlchemy Session 事务生命周期](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)；本地红绿测试验证项目边界。

教训：错误分类必须覆盖操作和收尾两条出口；清理失败不能抹掉提交确认。全量与真实数据库结果见 [F05a 评审](../../../docs/history/completed-work.md#db-f05a-review)，契约见[会话 Store](../../../docs/infrastructure_doc/database_doc/session_store.md)。
