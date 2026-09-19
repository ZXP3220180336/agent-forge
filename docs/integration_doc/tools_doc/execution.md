# 工具真实执行与接管

> 对应代码：`app/integration/tools/execution.py`
> 文档定位：内部协作、真实完成与资源责任
> 更新日期：2026-09-19
> 状态与测试映射：[ALIGNMENT](../../ALIGNMENT.md)

## 职责

`ToolExecutionSupervisor` 是工具任务及其后台线程的进程内拥有者。调用方停止等待后，它仍负责保存句柄、接管迟回结果和归还执行名额。Executor 决定是否重试、如何解释 ToolResult；Supervisor 不负责业务重试、协议历史、数据库或副作用核验。

`ToolAttemptHandle` 代表一次真实尝试。与调用方的等待状态分开，是因为协程收到取消并不证明线程已经停止。配置通过 Container 显式构造的冻结 `ToolExecutionSettings` 注入，参数正文见[配置参考](../../config_doc/config.md#tool-lifecycle-p0)。

## 内部协作契约

| 入口 | 行为与调用方责任 |
| --- | --- |
| `start(factory, *, call, permit, owner, on_complete, on_transfer, on_release)` | 在启动前占用跟踪条目；容量不足或宿主关闭返回 None，此时调用方仍须释放 Permit。成功返回 Handle 后，Permit 的释放责任转给 Supervisor。 |
| `wait(handle, *, timeout)` | 竞争真实完成、业务取消、deadline、run_stop 和单次超时；退出前完成接管或移交。 |
| `handle.check_abort()` | 按取消、deadline、run_stop 裁决；已经结束或正在停止的 Handle 禁止启动新工作。 |
| `handle.run_sync(fn, ...)` | 在宿主线程池执行一个完整同步操作，保留真实 concurrent Future。同一 attempt 不并行派发多个线程工作。 |
| `owns(tool)` | 判断该实例是否仍被真实工作使用，供热卸载保护。 |
| `records` | 有界接管记录快照。移交后不继续通知旧 Domain sink；迟回协程值和线程值分别留在 `value`、`thread_results`。 |
| `close()` | 停止新接管、请求在途任务取消并有界等待；仍未真实结束则抛 `ToolShutdownIncompleteError`，调用方不得关闭其依赖。 |

三个事实回调均同步且不得执行 I/O。`on_complete` 在任务和所登记线程全部结束后接管结果；`on_transfer` 在清理窗口耗尽时接管未决状态；`on_release` 只释放串行锁等本地资源，不能持有 Agent 或事实收集器。回调出错置位该 run 的停止信号并保留记录，不能变成业务重试。

观测任务可显式传 `permit=None`：只占有界宿主跟踪条目，不抢业务名额，使排队拒绝也有机会留审计；它不能使用 `run_sync` 派发线程。业务工具尝试和受控审批必须持有 Permit。

## 当前启用边界

`read_execution_spec(describe, parameters)` 统一隔离执行与导出入口的声明故障：普通异常或非 `ToolExecutionSpec` 返回值回落默认 UNKNOWN，由既有门禁拒绝或隐藏。取消、截止时间及运行停止控制异常继续传播；不捕获 `BaseException`。声明失败不启动真实调用，也不污染完成回调或同运行后续调用。

`is_execution_enabled(spec)` 是正式执行及模型导出共同使用的判断：只接受明确的 `ToolEffectClass.READ_ONLY` 且 `audit_required is False`。MAY_WRITE、UNKNOWN 或要求强制审计的能力需等待交付 B；不以风险等级、审批放行或已登记线程替代必要保护。

两种模型协议只导出 `describe_execution({})` 能证明可用的工具；依赖实参而无法预判时保守隐藏。实际调用按实参在审批前、每次尝试排队前及取得串行锁后复核；能力失效返回 REJECTED，未开始的事实为 NOT_STARTED/NONE/NOT_NEEDED，已取得的本地锁和 Permit 正常归还。不会增加实际尝试计数或抹除先前已执行尝试的事实。

注册清单仍保存未启用工具，门禁不阻止可信插件导入/初始化，也不是任意 Python 代码沙箱。直接调用适配器 `execute` 属于独立测试/内部接口，不是正式网关启用方式。移除 B 门禁必须随持久保护及其验收实施，不提供配置绕过。

## 执行与清理

```text
取得 Permit → start 预留跟踪条目 → invoke
  ├─ 协程与登记线程全部完成 → 接管结果 → 释放串行锁与 Permit
  └─ 取消 / deadline / 单次超时 / 硬取消
       → 请求协程取消 → 等待有限清理窗口
          ├─ 迟回完成 → 接管值/异常 → 释放资源 → 保留原终止语义
          └─ 仍未结束 → TRANSFERRED → 断开 Domain sink
                         → 后台真实完成 → 保存迟回值 → 释放资源
```

清理等待取配置预算与 `call.cleanup_deadline` 的较小值。再次硬取消不会让句柄或 Permit 失去拥有者。`asyncio.wrap_future` 的取消状态不作为线程完成证据；真实 Future 的完成回调通过事件循环线程接管。

`ToolAttemptTimeoutError` 只表示本地等待超时；工具自己抛出的 `TimeoutError` 保留原异常来源。超时期间迟回成功时，Executor 保留成功事实，但对外仍返回本地 TIMEOUT；业务取消和期限保持 shared 类型化异常。硬取消保持 `CancelledError`。

## 容量与边界

跟踪条目在调用前预留，不在取消之后临时找容量。正常完成且交付成功的记录可释放；移交、接收方未确认或交付失败的记录留在有界记录集中，满后拒绝新调度，不丢弃历史腾位置。当前没有持久恢复或自动删除这些记录的能力，后续消费路径属于生命周期后续切片。

`run_sync` 的等待被取消后，线程原始值或异常尚未交给适配器解释，记录继续保留；即使线程在清理窗口内完成、协程的失败事实已被确认，也不能删除该记录。真实完成仍释放 Permit；保留记录占用恢复条目，不改变对外取消或 TIMEOUT 语义。原始线程值不自动提升为工具成功结果。

真实在途线程继续占用业务 Permit，即使旧 Agent 已结束。停止接收工作、等待清理结束和强制终止进程是不同能力：本组件不能杀死 Python 线程，也不保证停止任意外部插件自行创建的线程/子进程。可信适配器必须通过受控入口登记工作；资源联合准入、持久账本、进程树和宿主强退分别按后续 Piece 实施。

## 验证与设计依据

- `tests/unit/test_tool_execution_supervisor.py`：迟回值、真实线程、接管上限、二次取消、关闭未排空、完成后禁止复用句柄。
- `tests/unit/test_tool_enablement.py`：执行/导出门禁、审批/重试/串行等待期间声明变化。
- `tests/unit/test_tool_write_execution.py`：写适配器隔离测试覆盖建目录、打开、写入及关闭阶段的取消/超时、真实容量和串行锁保留；不证明 B 已启用。
- `tests/unit/test_tool_attempt_lifecycle.py`：真实 Executor 链路的容量保留、事实接管、退避和观察预算。
- [TOOLS-ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)：所有权、适配器边界和分批保证。
- [Executor](executor.md)、[共享准入](admission.md)、[ToolService](tool_service.md)：结果解释、容量及依赖关闭的协作方。
