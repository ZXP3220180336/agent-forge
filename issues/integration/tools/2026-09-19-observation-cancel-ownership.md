# TOOLS-054：吞取消的异步观测拖住工具收尾

日期：2026-09-19；状态：已修复；优先级：P2。范围：ToolExecutor 的异步审计及 Hook。来源：C-02 Piece④ 生命周期复核。

## 现象与证据

真实工具已经成功，但注入的异步审计或 Hook 捕获 CancelledError 后继续等待，会使调用超过观察预算仍不返回。`test_tool_attempt_lifecycle.py::test_resistant_observation_is_bounded_and_keeps_owner` 的 audit/hook 两种参数在修复前都失败：经过观察窗口，外层任务仍未结束。

## 根因与方案

`asyncio.wait_for` 需要等待被取消的协程完成取消处理，其 timeout 不能证明等待时间严格有界。依据 [Python 官方等待原语说明](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for)，采用已批准的 [TOOLS-ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md) Owner 机制：用有界 wait 控制调用方等待，同时保留真实任务，不把取消请求当作完成。

Executor 将异步观测交给 Supervisor，启动前占用有界跟踪条目。观察 deadline 同时作为其清理截止，不追加宽限；超时仍未退出则移交，真实结束前保留实例依赖。观测不抢业务 Permit，从而准入拒绝仍可尝试记录；无 Permit 的 Handle 不允许派发线程。观察容量不足或预算耗尽可以跳过非关键观测，不能以记录成功作为业务结果前置。

## 验证与限制

上述两条红测转绿，并验证主结果成功、未退出观察任务仍有 Owner，放行后才能完成关闭。Supervisor 专项另验证无 Permit 的任务不能派发线程，以及跟踪上限不因观测而失效。运行命令与本轮完整验收见 [todo](../../../docs/todo.md#c-02-implementation-pieces)。

同步观测仍必须遵守非阻塞契约；本次不能强制中断堵住事件循环的任意 Python 代码。持久恢复、宿主强退不在本 Issue 的保证内。

## 教训

超时取消与严格等待上限是两项不同保证。非关键观测要同时有退出预算和后台任务拥有者，不能只增加一层 wait_for；正式约束见 [G0-3/G0-6](../../../docs/engineering/ai-engineering-rules.md#g0)。
