# 迁移收尾覆盖已选定主失败

> **ID**：DB-014 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P2
> **来源/范围**：DB-F03b 开发阶段独立复审；未发布、未执行真实迁移。

## 现象与复现

主终态与收尾结果共用可改写字段，两个路径覆盖已有失败：父进程拒绝非法消息后排空管道收到迟到成功，将 `worker_protocol_error` 改成 `ok`；worker 权限失败后清理收到取消，将 `permission_denied` 改成 `worker_failed`。

分别新增确定性测试 `test_protocol_failure_cannot_be_overwritten_by_late_success` 和 `test_worker_cleanup_cancellation_preserves_main_failure`，均先失败；前者实际 ok，后者实际 worker_failed。

## 根因与修复

父进程独立保存自己选定的终态，接管迟到确认事实后重新合并，不允许消息覆盖失败。worker 外层只在尚未选定失败时归类新异常，已有提交未知和主失败优先；收尾状态单独保留。遵守 [G0-4/G0-6](../../../docs/engineering/ai-engineering-rules.md#g0)。进程强退不执行 finally 的语义参照 [Python multiprocessing](https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Process.terminate)，不能将退出视为回滚证明。

## 验证与边界

两项复现均转绿，真实 spawn 另覆盖确认后挂死、待确认提交挂死、崩溃和坏消息；无真实数据库原子性证明。全量证据见 [F03b 评审](../../../docs/todo.md#db-f03b-review)，当前契约见[迁移说明](../../../docs/infrastructure_doc/database_doc/migrations.md#离线命令生命周期)。
