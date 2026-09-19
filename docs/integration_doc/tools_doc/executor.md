# 工具执行器说明

> 对应代码：`app/integration/tools/executor.py`
> 文档定位：内部编排、结果接管与重试边界
> 更新日期：2026-09-19
> 状态与测试映射：[ALIGNMENT](../../ALIGNMENT.md)

## 定位与阶段

ToolExecutor 负责固定实例后的准备、单次尝试、结果解释和观测收尾；不直接管理线程池或持久恢复。真实句柄由 [Supervisor](execution.md) 接管，容量由 [Admission](admission.md) 管理，公开调用签名由 [ToolService](tool_service.md) 维护。

```text
begin_call：预登记 NOT_STARTED → 入口检查
_prepare_call：解析参数 → Schema 校验 → 能力启用门禁 → 受控审批
_execute_with_retry：逐次准入 → _run_attempt → 先接管事实 → 复查控制
  ├─ 成功 → _complete_call 仅处理展示副本
  ├─ 不可重试 / 次数耗尽 / 前次未真实结束 → 返回
  └─ 允许重试 → 释放前次资源 → 可中断退避 → 下一次准入
最后：有界审计与 Hook → 返回前控制复查
```

## 调用前的准入

每次 attempt 单独取得 Permit，退避不占业务名额。首次准入预算从 Facade 入口计算，包含插件刷新、审批和排队；刷新已耗尽预算时，准备阶段直接返回 `CAPACITY_EXCEEDED`。后续 attempt 从退避结束重新计算准入窗口，业务绝对 deadline 不重置。

工具实例在整次调用期间固定。准备、审批、退避以及 Supervisor 接管后的后台任务都阻止卸载。`concurrency_safe=False` 的工具在真实任务内取得串行锁，锁与 Permit 一起由 Supervisor 在真实完成后释放；不能在等待者退出时提前释放。串行锁等待位于已取得 Permit 的尝试内，受单次工具 timeout 和业务 deadline 限制；当前不使用共享队列的 admission deadline，此边界不能描述为全部调用前等待使用同一个准入窗口。

审批同样由 Supervisor 持有，其未结束不等于工具已经执行。取消、期限、run_stop 的优先级由 `execution.check_abort` 统一维护；准入组件保留其队列边界检查。未开始的拒绝发布 `NOT_STARTED/NONE`，实际尝试次数为零。

执行前按[当前启用边界](execution.md#当前启用边界)校验能力：审批前、每次 attempt 排队前及拿到串行锁后均复核。首次拒绝零实际执行、零业务 Permit；等待中声明失效时释放已取得的 Permit/锁，发布 NOT_STARTED/NONE/NOT_NEEDED。审批不能替代持久保护，能力拒绝不重试。

## 调用后的事实与结果

每次 attempt 都有独立 ID，重试共享 operation_id。真实结果的类型与消费字段先在边界验证，然后发布 attempt 和操作快照，最后才决定取消、期限、展示或重试。操作快照版本随 attempt 单调增加。发布顺序固定为 Integration 自有副本 → Domain 独立副本；sink 失败置位 run_stop、保留事实并原样传播。

适配器返回值必须是 ToolResult，且 `success/content/error/error_code/effect_state` 符合字段契约；非法返回收敛为 UNKNOWN 失败且不重试，不做静默强转。`metadata` 为扩展字段，`execution_time/retry_count` 由执行器填写。成功展示截断使用副本，处理失败保留原结果，不重新执行工具。

| 出口 | 事实与对外行为 |
| --- | --- |
| 正常成功或失败 | 先发布实际结果；清理 COMPLETE，然后按结果和安全声明决定是否继续。 |
| 工具自己抛 TimeoutError | 本地协程已完成则清理 COMPLETE；效果由可信声明和已接管证据决定（只读为 NONE），不从错误名称推导线程仍在运行。 |
| 本地单次超时 | 返回 TIMEOUT，不自动重试；清理期间取得的迟回成功单独保留在事实中，不能被 TIMEOUT 展示结果覆盖。 |
| 业务取消/期限/run_stop | 完成或移交回调先保存事实，再传播 shared 类型化异常。 |
| 硬取消 | 同样请求有界清理/移交，保留 CancelledError；不保证失去消费方后继续向它交付。 |
| 清理未完成 | TRANSFERRED；后续迟回值由 Supervisor 保存，不再引用旧 Agent 或 sink。 |

执行状态、本地清理状态和远端效果分别解释：本地任务结束不证明远端没有副作用。正常交付完成的快照可从 Executor 内存释放；移交及未接管事实不得按调用方退出直接丢弃。

## 重试和观测

`max_retries` 沿用公开名字，但语义为最大实际尝试数、包含首次；调用方传零沿用既有至少尝试一次语义。次数 Owner 是 Executor 单一循环。只有失败可安全重复、次数有余额、前次真实完成、控制未命中四项都满足，才进入指数退避；BaseTool 默认不重试。参数/审批/排队拒绝、事实交付错误、成功后的处理错误不触发重放。SDK 内部重复路径需由适配器单独核验。

统计、审计与 Hook 消耗单次调用的共同观察预算。同步观测必须是非阻塞计算；异步审计和 Hook 在 Supervisor 有界跟踪下运行，吞取消时移交，不能拖住主结果。观测占用跟踪条目但不抢业务 Permit，也无权派发线程；接管容量不足或预算耗尽可以跳过非关键观测。审计尽力覆盖正常、失败和未执行拒绝出口，不作为持久恢复账本。

`close()` 先撤回排队、设置活动调用的 run_stop，再排空真实工作并等待准备/退避调用退出。未完成则向 ToolService 抛出关闭未完成异常，依赖必须保留。

## 验证与关联

- `test_tool_executor_components.py`：校验、串行化、截断、审计和容量拒绝。
- `test_tool_fact_ownership.py`：先接管事实、独立副本、已完成与未知效果的区分。
- `test_tool_attempt_lifecycle.py`：真实线程、迟回值、退避、取消/期限与有界观测。
- `test_tool_enablement.py`：未就绪能力拒绝、导出过滤与等待后复核。
- `test_tool_lifecycle_contract.py`：控制优先级和入口事实。

测试文件均位于 `tests/unit/`。配置正文见[工具生命周期配置](../../config_doc/config.md#tool-lifecycle-p0)，设计取舍见 [TOOLS-ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)。本组件的进程内能力不代表后续持久恢复、联合资源保护或完整领域批次提交已完成。
