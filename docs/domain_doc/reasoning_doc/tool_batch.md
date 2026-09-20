# 工具批次事实收集说明

> **对应代码**：`app/domain/reasoning/tool_batch.py`
> **文档定位**：组件说明——Domain 内部事实所有权与批次隔离契约
> **更新日期**：2026-09-20
> **状态与映射**：[ALIGNMENT](../../ALIGNMENT.md) 中 `app/domain/reasoning/tool_batch.py` 行

## 目录

- [工具批次事实收集说明](#工具批次事实收集说明)
  - [目录](#目录)
  - [设计目标与边界](#设计目标与边界)
  - [内部协作契约](#内部协作契约)
  - [核心概念与机制](#核心概念与机制)
    - [事实的身份与版本](#事实的身份与版本)
    - [两条信息流](#两条信息流)
    - [批次宽限（`stop_at`）](#批次宽限stop_at)
  - [结构与协作](#结构与协作)
  - [状态与执行流程](#状态与执行流程)
    - [四个阶段](#四个阶段)
    - [`while pending:` 的每一轮](#while-pending-的每一轮)
    - [三条退出路径](#三条退出路径)
    - [收尾阶段对未退出任务的处理](#收尾阶段对未退出任务的处理)
  - [关键实现说明](#关键实现说明)
    - [① 三个不变量](#①-三个不变量)
    - [② 两处 `remaining` 的 `None` 语义相反](#②-两处-remaining-的-none-语义相反)
    - [③ `max(0.0, ...)` 是语义规整，不是防崩](#③-max00--是语义规整不是防崩)
    - [④ `wait` 与 `wait_for` 的超时语义不同](#④-wait-与-wait_for-的超时语义不同)
    - [⑤ 领域包装任务 ≠ 真实执行](#⑤-领域包装任务--真实执行)
  - [行为边界](#行为边界)
  - [配置关联](#配置关联)
  - [验证入口](#验证入口)
  - [设计决策与问题记录](#设计决策与问题记录)
  - [相关文档](#相关文档)

## 设计目标与边界

- `ToolBatchCollector` 是一次工具批次的可见事实 Owner：Integration 通过同步 `record()` 发布事实，ReAct 在结束或异常传播前通过 `snapshot()` 接管当前可得事实，并调用 `close()` 断开后续更新
- `ToolBatchRunner` 只负责本批次的并行任务、逐项结果接管及有界兄弟收尾。

**不在本组件职责内**：执行工具、持有共享许可或真实句柄、写数据库、提交协议历史。真实线程与迟回效果由 Integration 的 `ToolExecutionSupervisor` 持有；协议回执与 assistant.tool_calls 由 ReAct 提交。

## 内部协作契约

| 方法 | 行为 | 调用方责任 |
| --- | --- | --- |
| `record(fact)` | 按 batch、当前 call、规范 operation、attempt 建键；只有更高 revision 覆盖旧快照 | Integration 必须先保留自己的副本，再同步通知 |
| `snapshot()` | 返回深复制的有序 tuple | 调用方可保存或修改自己的副本，不会污染 collector |
| `close()` | 幂等停止接收晚到更新，保留已经接管的快照 | 批次正常结束、类型化终止和生成器关闭都应调用 |
| `ToolBatchRunner.run(calls, execute, cleanup_deadline=...)` | 在启动任何子任务前预登记全部合法调用；逐项接管返回或异常，按输入顺序返回结局 | ReAct 使用同一批次上下文、接管快照并选择终态；不把异常改写成正常业务成功 |

同一个规范 operation 被多个消费 call 引用时（业务幂等键复用启用后；当前实现为每个 call 生成独立 `operation_id`，尚不存在该路径），`batch_id/tool_call_id` 仍保持各自协议身份，不能合并为一条工具消息。Runner 在单个任务返回时接管其结局，不等整批成功才确认；控制异常出现后，给在途兄弟 `min(now + 批次宽限, cleanup_deadline)` 的有界收尾机会，再取消仍未退出的领域等待任务。Collector 关闭后不再接收晚到更新。正常工具失败不取消兄弟，也不自动重复执行。

ReAct 根据已接管事实为每个 call 构造成功、失败、未执行或结果未知的协议回执。预登记的 NOT_STARTED 不得盖过已完成的 attempt 事实；回执按模型输入顺序连同 assistant.tool_calls 在首条工具 SSE 事件前提交。业务控制异常仍类型化上抛，不能为补齐历史启动新的 LLM 或工具调用。外部消费者提前关闭生成器时，已提交的历史保持完整；尚未开始批次时不提前写入 assistant.tool_calls。

## 核心概念与机制

### 事实的身份与版本

`record()` 以 `(batch_id, tool_call_id, operation_id, attempt_id)` 建键，`revision` 单调递增决定覆盖：只有更高 revision 才替换旧快照。`snapshot()` 返回深复制，调用方修改自己的副本不会污染 collector。`close()` 之后 `record()` 返回 `False`，不再接管晚到事实。

建键里的两组字段代表**两种不同身份**，维护时不可混用：

| 身份 | 字段 | 语义 | 决定什么 |
| --- | --- | --- | --- |
| 协议身份 | `batch_id` + `tool_call_id` | 模型发出的一次 tool_call | 回执条数与配对，不能被合并 |
| 业务身份 | `operation_id` | 一次逻辑操作，重试共享该身份 | 事实归属与重放保护 |

当前 ReAct 为每个 call 生成独立 `operation_id`，两者恒为 1:1。业务幂等键复用（[ADR S1](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)，Piece B 未实现）启用后会变成 1:N：同一个 `operation_id` 被多个消费 call 引用，而 `batch_id`/`tool_call_id` 仍各自独立。建键已按此设计——即使 `operation_id` 与 `attempt_id` 相同，协议身份不同就是不同事实、不同回执，因此两条 call 不会被折叠成一条工具消息。

### 两条信息流

批次执行期有两条流并行推进，而**回执只取自事实流**：

- **返回值流**：子任务返回值或异常经 `absorb` 进入 `outcomes`，代表领域侧"拿到了什么"。
- **事实流**：Integration 每产生一个事实就同步调用 `collector.record()`，代表"真实执行发生了什么"。

`outcomes` 中的 `None` 或异常对象都不构成成功证据，ReAct 生成回执时一律回落到事实流。这是"禁止编造成功"的落点。

### 批次宽限（`stop_at`）

兄弟调用报出三类控制异常后，批次仍需给在途兄弟一段收尾时间，使它们的成果有机会进入协议历史。这段时间的绝对截止点记在 `stop_at`：

```python
stop_at = min(time.monotonic() + _BATCH_CLEANUP_GRACE, cleanup_deadline)
```

`stop_at is None` 表示尚未有控制异常，此时无界等待。两个性质：`stop_at is None` 守卫（收集兄弟异常与处理外层硬取消两条路径都带）保证宽限**不叠加**，已起算的窗口不会被硬取消重取一份；`min` 保证批次等待**不越过**运行硬取消时刻。

## 结构与协作

一次批次从 ReAct 到真实执行的调用链（`text` 结构图，非可运行代码）：

```text
_execute_one(index)                       (react.py)
 ├─ json.loads 参数解析
 │    └─ 失败 → collector.record(JSON_PARSE 事实) → 直接返回，零真实调用
 └─ await gateway.execute(...)            (生产装配下为 ToolService)
      └─ Executor.execute
           ├─ begin_call / check_abort     ← 调用身份；取消/期限/run_stop 检查
           ├─ _prepare_call                ← 固定实例、解析参数与执行声明
           │    └─ _request_approval       ← 仅审批工具：准入与审批占同一窗口
           │         └─ ToolAdmission.acquire
           ├─ _execute_with_retry
           │    └─ _run_attempt
           │         ├─ ToolAdmission.acquire  ← 每次尝试（含重试）重新准入
           │         ├─ supervisor.start   ← 真实执行任务/线程
           │         └─ supervisor.wait    ← 有界等待真实结果
           ├─ _publish_fact                ← 事实同步进入 collector
           └─ check_abort                  ← 出口复查控制信号
```

`await asyncio.wait` 期间事件循环交错推进全部子任务（并非顺序执行），这是并发真正发生的地方。

## 状态与执行流程

### 四个阶段

| 阶段 | 代码锚点 | 做什么 |
| --- | --- | --- |
| ① 预登记 | `for call in calls:` → `self._collector.record(ToolFact(revision=0, ...))` | 为每个 call 写入 `revision=0` 的 `NOT_STARTED` 事实，让"从未启动"也有事实可查；`revision=0` 保证任何真实事实（`revision>=1`）都能覆盖它 |
| ② 并发启动 | `tasks = {asyncio.create_task(execute(index)): index ...}` | 一次性创建全部子任务，`pending` 指向全部，`stop_at = None` |
| ③ 逐项接管 | `while pending:` 循环 | 完成一个接管一个，不等整批 |
| ④ 有界收尾 | `finally:` + `if pending:` | 取消未退出的，有界等待，注册兜底回调 |

`create_task` 只是把协程排进事件循环就绪队列，此刻一行都还没执行；真正开跑发生在第一次 `await` 让出控制权之后。`tasks` 以 Task 为键反查 index，因此结局按**输入顺序**落位，与完成顺序无关。

### `while pending:` 的每一轮

1. **算剩余**：`remaining = None if stop_at is None else max(0.0, stop_at - time.monotonic())`。`stop_at` 为 `None` 表示还没有任何兄弟报控制异常，此时 `timeout=None` 即无限等；有值则是"宽限还剩多久"。
2. **让出**：`await asyncio.wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)`——循环内唯一让出事件循环的地方。
3. **接管**：`absorb(done)` 把完成值或异常对象写进 `outcomes[index]`。纯同步，不让出事件循环，所以宽限计时不会被自身处理逻辑拖长。
4. **判断**：`if not done: break`。`asyncio.wait` 超时不抛异常、不取消任务，只返回空 `done`；这是宽限耗尽的唯一出口。

宽限启动的条件是 `isinstance(error, _CONTROL_ERRORS) and stop_at is None`：前者保证只有三类业务控制异常才触发计时，后者保证多个兄弟同时报异常时不会反复重置宽限。

### 三条退出路径

| 路径 | 怎么走出 | 进入 `finally` 时 `pending` | 该路径的 `remaining` |
| --- | --- | --- | --- |
| 全部完成 | `pending` 空，while 条件为假 | 空 | 不进分支 |
| 宽限耗尽 | `done` 空 → `break` | 可能非空 | `0`，取消后不再等待 |
| 外层硬取消 | `CancelledError` → 尚无宽限时才设 `stop_at = now + 宽限` → `raise` | 可能非空 | 约等于剩余宽限 |

### 收尾阶段对未退出任务的处理

`task.cancel()` 取消的是**领域包装任务**，不是真实执行。随后按 `remaining`（宽限耗尽路径为 `0`，外层硬取消路径为剩余宽限）有界等待一次，被取消任务的结局分两类（2026-09-20 以 `timeout=0` 实测）：

- **合作取消**的任务会被这次轮询捕获，`asyncio.CancelledError` 写入 `outcomes`；
- **吞掉取消**的任务留在 `pending`，`outcomes` 保持 `None`，并注册 done 回调取出异常，避免 asyncio 报 "exception was never retrieved"；该回调只接 `finished` 参数，不保留对 Agent 的引用。

两类在 ReAct 都走事实分支生成回执（未启动→「未执行」，已启动→「结果尚未确认」），所以**回执口径不受是否捕获影响**。真实执行的停止与移交由 Integration 的 `ToolExecutionSupervisor._stop` 负责：它有自己的一级清理窗口，并取与 `cleanup_deadline` 的较小值，不阻塞 Agent。

## 关键实现说明

以下五点维护者最容易误读，且都不改变契约。

### ① 三个不变量

1. `absorb` 是唯一写 `outcomes` 的地方，且只写已完成的——`outcomes` 永远是"已确认 + None（未确认）"的混合，未完成项不会被编造。
2. `stop_at` 只被设置一次（`stop_at is None` 守卫）。
3. 循环体内只有 `await asyncio.wait` 一处让出事件循环，故宽限是真实的墙钟上界。

### ② 两处 `remaining` 的 `None` 语义相反

循环内 `stop_at is None` 要表达"无限等"，故用显式 `if/else` 返回 `None`；收尾处要表达"一秒不等"，故用 `or` 得到 `0`。收尾处那句 `(stop_at or time.monotonic())` 是 **falsy 判断而非 None 判断**，本行安全仅因 `time.monotonic()` 不会为 0。

### ③ `max(0.0, ...)` 是语义规整，不是防崩

实测 `asyncio.wait(timeout=-1.0)` 不报错，行为等同 `timeout=0`。

### ④ `wait` 与 `wait_for` 的超时语义不同

`asyncio.wait` 超时只返回空 `done`，不抛异常也不取消任务；`asyncio.wait_for` 超时会抛 `TimeoutError` 并取消目标任务。本组件的宽限到期依赖前者。

### ⑤ 领域包装任务 ≠ 真实执行

**术语**：领域包装任务指 `run()` 用 `asyncio.create_task` 包住一次工具调用的那个任务，即 `pending` 集合的成员。它自己不执行工具，只负责调用 `gateway.execute(...)` 并把返回值或异常交给 `absorb`。

**三层结构**：领域包装任务（本组件）→ 集成任务（`Supervisor.start` 建立的真实尝试任务）→ 真实线程（Supervisor 线程池里的 concurrent Future）。

**取消边界**：`task.cancel()` 只作用于最上层——它在 `_execute_one` 的 await 点抛入 `CancelledError`，沿 await 链传到集成的 `Supervisor.wait` 即被接管，**到不了真实线程**。所以"取消领域任务"只表示领域不再等待，不表示底层执行已停。机制、资源责任与迟回值归属见[工具真实执行与接管](../../integration_doc/tools_doc/execution.md)。

**内存边界**：`_execute_one` 定义在 `ReActStrategy.execute_tool_calls` 内部，闭包只捕获被引用到的变量。代码以 `gateway = self._tools` 提升，使未合作取消的包装任务不引用 Agent；配合收尾处的 done 回调取走异常，避免 asyncio 报 "exception was never retrieved"。

## 行为边界

| 触发场景 | 行为 | 约束 |
| --- | --- | --- |
| 普通工具失败 | 只影响该调用，不取消兄弟、不自动重试 | Runner 不把异常改写成成功 |
| 单工具控制异常 | 先接管其事实，再给兄弟 `min(now + 宽限, cleanup_deadline)` 收尾 | 宽限只启动一次 |
| 宽限耗尽 | 取消未退出的领域包装任务，不再等待 | 真实执行仍归 Integration |
| 外层硬取消 | 记录 `stop_at` 后原样传播 `CancelledError` | 不伪装业务失败 |
| 参数 JSON 解析失败 | 发 `JSON_PARSE` 事实并直接返回 | 零真实调用 |
| 消费者提前关闭生成器 | 已提交的协议历史保持完整 | 批次未开始时也不预写 assistant.tool_calls |
| Collector 关闭后 | `record()` 返回 `False`，不再接收晚到更新 | 迟回事实归 Integration 与账本 |

## 配置关联

本组件**不直接读取配置**：`cleanup_deadline` 由领域外层（ReAct）按运行硬取消时刻派生后传入。批次宽限目前是模块常量 `_BATCH_CLEANUP_GRACE`，不受配置控制；集成侧每次真实调用的清理等待由[配置参考](../../config_doc/config.md#tool-lifecycle-p0)的 `tool_cleanup_timeout_seconds` 维护，两者在各自作用域内独立生效。该差异已记录待处理，见 [todo](../../todo.md)。

## 验证入口

`tests/unit/test_tool_lifecycle_contract.py` 覆盖 revision 幂等、关闭边界和双向快照隔离；`tests/unit/test_tool_lifecycle_wiring.py` 覆盖部分成功加三类控制异常、未知回执、真实 ToolService 并行取消、提前关闭与协议配对。当前实现、接线与验证状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准。

## 设计决策与问题记录

- [TOOLS-ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)：S1（公开接口及事实所有权）、S3（文件、类与方法职责）、S4（实际消费方与批次执行接口）、S8（方法级验收）定义本组件的正式契约；D4（沿现有清理窗口规则、不重新累计宽限）与 D5（批次收集器协调兄弟、不机械取消）解释宽限与收尾语义。
- 本组件的未处理项集中在 [todo](../../todo.md) 的 C-02「Piece⑤ 复核遗留」，本文不复制清单。

## 相关文档

- 父导航：[领域层文档](../README.md)
- 协作组件：[ReAct 策略说明](react.md)（协议回执与终态提交方）
- 跨层协作：[工具真实执行与接管](../../integration_doc/tools_doc/execution.md)（真实线程、取消语义与迟回值归属）
- 配置：[配置参考](../../config_doc/config.md#tool-lifecycle-p0)
- 状态：[对齐表](../../ALIGNMENT.md)
