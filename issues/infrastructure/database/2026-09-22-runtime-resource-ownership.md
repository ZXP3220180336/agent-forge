# 数据库关闭未完整保留资源 Owner

> **ID**：DB-003 · **日期**：2026-09-22 · **状态**：已修复 · **优先级**：P1
> **发现来源**：DB-F02 初版实现与独立生命周期审查；尚未接入应用或发布。
> **范围**：DatabaseRuntime Session/连接登记与并发 dispose。

## 现象与证据

初版只检查已经 checkout 的连接，遗漏驱动初始化中及尚未连接的 Session，dispose 可提前报告 closed。两个并发 dispose 等待同一失败任务时，一个清空共享引用，另一个访问该引用导致 AttributeError。三项行为均先补回归并跑出失败，再修复。

## 根因与取舍

资源 Owner 覆盖范围小于资源实际生命周期，且等待者依赖可变成员而非自己等待的任务。根据 [SQLAlchemy 异步生命周期](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) 与 [pool 事件](https://docs.sqlalchemy.org/en/20/core/events.html#connection-pool-events)，不能把未 checkout 或 dispose 返回当作没有业务 Owner。

采用私有 AsyncSession 子类，从工厂创建起登记到 close 成功；close 失败/取消、上下文 shielded close 未完成仍保留责任，禁止关闭后重新开启 Session。另以 connect/checkout/checkin 跟踪驱动初始化和借出事实。关闭不得抢占业务 Owner；等待任务存局部引用并按 identity 清理共享引用，不建设通用资源管理框架。

## 实施与验证

`tests/unit/test_database.py` 对提前关闭和并发失败先红后绿。独立审查新增 `tests/unit/test_database_lifecycle_review.py`，用真实 SQLAlchemy Session 验证 close 取消、上下文后台关闭及关闭后复用拒绝，三项通过。其他挂起、迟到、取消与驱动物理释放检查见 [DB-F02 评审](../../../docs/history/completed-work.md#db-f02-review)。

末次复核另补 ping 超时且探针 Owner 未退出的红测：ping 不更新健康快照，但不能因此放任新工厂调用或 checkout。修复后资源准入同时检查未完成的探针清理，仍保留原有 ping 只观测语义。

清理阶段挂起也不能覆盖已经接管的主失败：补“认证失败后 close 超时”的红测，原实现返回 timeout；现保存稳定主原因码，有限清理后仍报告 authentication_failed，并继续保留未结束的资源任务。

## 边界与关联

仍由使用方先排空事务再关闭 Session；本片不实现 ChatService/Container drain，不承诺强制退出进程。经验归 [G0-3](../../../docs/engineering/ai-engineering-rules.md#g0) 与 [数据库 ADR D6](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d6生命周期接线)，不新增重复规则。
