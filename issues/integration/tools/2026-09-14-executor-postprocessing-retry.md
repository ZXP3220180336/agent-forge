# TOOLS-050 成功后处理失败触发工具重放

> **状态**：已修复（2026-09-14）
> **优先级**：P1
> **发现来源**：C-02 工具执行生命周期 Piece ①实施复核
> **范围**：`app/integration/tools/executor.py`、`base.py`、`hooks.py`、`tool_service.py`
> **关联决策**：[TOOLS-ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)

## 现象与影响

`ToolExecutor._execute_with_retry` 原先把真实 `tool.execute()`、成功结果截断、统计和 Hook 放在同一个 `try` 中。工具已经成功后，截断或统计抛出的普通异常会落入工具失败分支；在默认最大三次执行的配置下，同一工具可能被再次调用。所有普通失败、超时和异常也都自动重试，次数预算被错误地当作重复执行的安全依据。

该行为对写文件、执行代码和未知 HTTP 写入可能造成重复副作用。即使是只读工具，永久业务错误也不应仅因剩余次数而重复。AWS 的可靠性指导同样要求先确认操作具备幂等/安全契约，再启用有限重试，不能把重试次数本身当成安全证明：[Control and limit retry calls](https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_mitigate_interaction_failure_limit_retries.html)、[Making retries safe with idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)。

## 根因

重试异常边界覆盖了真实调用之后的处理阶段，并且 BaseTool 没有表达“本次失败是否可安全重试”的入口。执行事实、展示处理和非关键观测因而共享同一恢复动作。

## 修复方案与取舍

- `try/except` 只分类真实工具调用；调用成功后的截断、统计、Hook 不再进入工具重试分支。
- `BaseTool.can_retry(result_or_error)` 缺省返回 False。只有可信适配器显式声明安全且次数仍有余额，Executor 才进入下一次 attempt；判断逻辑自身失败时停止重试并保留原失败。
- 展示截断在结果副本上执行；失败时返回已取得的原结果。统计、Hook 和审计按非关键观测隔离，不覆盖主结果。
- Hook 每次取得独立参数/结果快照；异步 Hook 与审计共享单次观察预算，审计优先，超时停止后续 Hook。同步 Hook 只允许非阻塞计算，阻塞 I/O 必须改为 async。
- `retry_count` 继续表示实际执行次数；`max_retries` 继续沿用既有“最大执行次数（含首次）”口径。本次不改公开参数名。

本次没有为任何生产适配器宣称可安全重试，因此它们默认只执行一次。RCA、搜索和网页读取等工具的具体可重试错误、SDK 内部重试及真实远端请求上界，随 C-02 Piece ④逐适配器验证后接入。

## 实施记录

- `executor.py`：收窄调用异常范围，分离成功后处理；安全读取重试声明；隔离统计和审计。
- `base.py`：增加保守的 `can_retry` 扩展点。
- `hooks.py`：结果快照、异步观察总预算及失败隔离。
- `tool_service.py`：向 Executor 传递观察预算；配置系统接线仍归 Piece ③。
- `test_tool_executor_components.py`：复现截断失败重放，并覆盖默认禁止、显式允许、判断异常、Hook 超时/篡改、统计失败和审计挂起。

## 验证结果

修复前两条新增回归分别表现为工具被执行三次，以及未声明安全仍执行三次。修复后：

- Executor、Hook、审计、基础工具、Agent 及真实内置工具相关回归：94 passed。
- 全量测试：991 passed，1 条既存 Starlette/httpx 弃用告警。
- `scripts.verify_alignment` 与 `git diff --check` 通过。

本轮未为生产适配器开启自动重试，也未验证其 SDK 内部重试；该边界保留在 C-02 Piece ④。

当前观察上限依赖受信 Hook/审计协作响应 asyncio 取消；故意吞掉 `CancelledError` 的回调仍可能拖延返回。此类非协作任务的独立 Owner 与强制移交归 Piece ④，当前不能把普通 `wait_for` 宣称为物理终止保证。Python 也明确说明 `wait_for` 会等待取消完成，实际等待可能超过给定 timeout：[asyncio.wait_for](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for)。

## 可复用教训

重试 catch 只能包住真正允许重复的操作。响应已经取得后，解析、展示和观测失败需要各自的处理策略，不能重新进入外部副作用；次数上限和幂等安全是两个独立准入条件。
