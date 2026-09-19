# 工具共享准入说明

> **更新日期**：2026-09-19
> **模块**：`app/integration/tools/admission.py`
> **职责**：维护工具调用的全局/单运行在途容量、有界等待队列、按运行轮转和 Permit 释放。

`ToolAdmission` 位于 ToolExecutor 之外，避免每个 Agent 或每个 ToolService 实例各自创建
独立准入器。`Container` 只向 ToolService 注入全局/单运行限额等标量配置；ToolService 构造
唯一准入器并注入 ToolExecutor，因此同一进程内的多个 Agent 共享全局在途上限；单运行上限
仍按 `ToolCallContext.run_id` 隔离。

## 准入语义

```text
ToolExecutor
  → ToolAdmission.acquire(call, deadline=首轮准入绝对期限)
      ├─ cancel/deadline/run_stop → 抛类型化控制异常
      ├─ 队列达到全局或单运行上限 → 返回 None
      ├─ 准入等待超时 → 返回 None
      └─ 取得 Permit → start 成功后由 Supervisor 在真实完成时 release()
```

返回 `None` 会由 Executor 生成 `CAPACITY_EXCEEDED` 的 `ToolResult`，执行状态为
`NOT_STARTED`，不会进入工具重试，并按 execute 的正常退出点留审计（覆盖面规则见
[executor.md](executor.md)）。已经取得的 Permit 释放操作幂等。

合法状态转换为 `QUEUED → GRANTED → RELEASED` 与 `QUEUED → WITHDRAWN`。`close_run`
实现按运行的 WITHDRAWN：撤回该运行尚未取得的等待者（写入 `None`），不触碰已取得 Permit
的在途调用——后者由真实执行 Owner 在完成后释放。当前生产调用方只有全局 `close()`；按运行的
撤回随运行生命周期切片接入，在此之前不额外暴露调用点。

每个运行拥有 FIFO 队列，调度器在有可用容量时按运行轮转。单运行已达到在途上限时，
其他运行仍可取得全局剩余容量；资源声明的联合准入属于后续资源保护切片，当前空声明不
改变既有只读工具行为。

取消和绝对期限会直接竞争排队 Future，排队不会等到其他工具自然释放后才发现终止。撤回等待
者不会伪造已经取得 Permit 的执行完成；在途责任仍由执行器及 [Supervisor](execution.md) 负责。

首次准入可传入 Facade 计算的绝对 `deadline`，将插件刷新、审批和排队计入同一窗口；省略时从本次 acquire 开始计算配置等待上限。该参数与 `call.deadline` 的业务总期限分别解释，前者耗尽返回 None，后者传播类型化期限异常。

## 代码阅读指南

下面的说明对应 `app/integration/tools/admission.py` 的实际实现，帮助第一次阅读代码时
区分“等待队列”“调度顺序”和“执行名额”这几个概念。

### `setdefault` 不会清空已有队列

入队代码如下：

```python
queue = self._queues.setdefault(call.run_id, deque())
queue.append(waiter)
```

`setdefault` 的规则是：

- `run_id` 已经存在时，返回原来的 `deque`；
- `run_id` 不存在时，才创建并保存一个新的 `deque`。

因此已有请求会保留，新请求会追加到队尾。例如原队列是 `[A1]`，追加后是
`[A1, A2]`。只有直接执行 `self._queues[run_id] = deque()` 才会替换并清空原队列。

### Future 是一次性的准入通知

每个等待者包含调用信息和一个 Future：

```text
_Waiter
├── call   谁在申请工具执行
└── future 等待准入器写入结果
```

Future 本身不执行工具。`_pump()` 找到空闲名额时写入 `ToolPermit`，调用方的
`await waiter.future` 就会继续；准入器关闭并撤回等待者时可以写入 `None`。因此它比
只表示“发生/未发生”的 Event 更适合传递一次性的准入结果。

### `_pump()` 如何发放 Permit

`_pump()` 是准入器的发牌流程：

```text
还有全局空闲名额？
    ├─ 否：停止
    └─ 是：从 _run_order 取一个运行
              ├─ 没有等待者：清理该运行，继续
              ├─ 已达到单运行上限：暂时跳过，检查下一个运行
              └─ 取该运行队首 waiter
                    ├─ Future 已有终态（结果已定）：丢弃该等待者，继续找下一个
                    └─ 否则：增加全局/单运行在途计数 → Future 写入 ToolPermit → 结束本轮
```

队列里出现“Future 已有终态”的等待者时不能发放 Permit：它的准入结果已经确定，再写一次
既会覆盖结果，也会让在途计数平白多一个永不释放的名额。这类等待者与容量无关，因此丢弃
后继续轮转，而不是结束本轮。

同一个运行内部使用 FIFO；不同运行之间按轮转顺序选择。`_last_granted_run` 记录上次
放行的运行，使下一轮从其他运行开始，避免一个运行连续占满机会。外层循环只有在一轮
轮转“什么都没推进”（既没发放 Permit，也没丢弃等待者）时才退出，避免在只剩坏条目时
空转。

### `_queued_runs.discard(run_id)` 的含义

`_run_order` 保存“接下来要检查哪些运行”，`_queued_runs` 是防止同一运行重复登记的
辅助集合。`_pump()` 取出一个运行后，会先执行：

```python
self._queued_runs.discard(run_id)
```

这只删除“已登记”标记，不会删除 `_queues[run_id]` 中的等待者。如果该运行仍有请求，
后面会通过 `_enqueue_run(run_id)` 重新登记；如果已经没有请求，就不会重新加入。
`discard` 在元素不存在时不抛异常，适合取消、关闭和调度交错的清理路径。

### `Future.done()` 是即时放行的快速路径

入队后会立即调用 `_pump()`。如果当时有空闲名额，Future 可能已经被同步写入 Permit：

```text
创建 waiter → 加入队列 → _pump() 立即放行 → Future.done() 为 True
```

此时：

```python
if waiter.future.done():
    return await waiter.future
```

`done()` 只表示结果已经准备好；这里的 `await` 会立即取出结果，不会再次等待。这样可以
避免为已经取得结果的调用额外创建取消、deadline 和准入超时监听任务。

如果 Future 尚未完成，代码才会让以下事件同时竞争：

```text
等待 Permit
等待取消事件
等待 run_stop
等待绝对 deadline
等待准入超时
```

谁先完成就先处理谁。

### 为什么 `_pump()` 要跳过已经完成的 Future

在 `_pump()` 从运行队列取出 waiter 后，还会检查：

```python
if waiter.future.done():
    made_progress = True
    continue
```

这里的 `done()` 表示“准入结果通知已经结束”，不是“工具已经执行结束”。Future 可能
已经进入终态的原因包括：

- `_pump()` 之前已经给它写入了 `ToolPermit`；
- `close_run()` 撤回排队请求时给它写入了 `None`；
- 等待协程被取消，Future 进入取消状态；
- 关闭、取消和调度交错后，队列中暂时残留了已经失效的 waiter。

Future 一旦进入终态，就不能再次调用 `set_result()`。因此 `_pump()` 遇到这种 waiter
会跳过它，继续寻找仍然有效的等待者；否则可能重复发放 Permit 并触发
`asyncio.InvalidStateError`。

需要区分两种“完成”：

```text
waiter.future.done()
    = 已经决定调用能否进入工具执行阶段

ToolResult
    = 工具实际执行结束后产生的结果
```

所以这个检查只保护准入通知和 Permit 发放，不代表工具业务已经完成。

### `_finalize_waiter()` 为什么要处理竞态

取消和放行可能在同一时刻发生。例如：准入器刚把 Permit 写入 Future，调用方马上收到
取消信号。此时如果调用方没有拿到 Permit，却又不回收它，容量计数就会永久少一个位置。

`_finalize_waiter()` 统一完成三步：

```text
取消并等待监听任务
    → 从等待队列移除 waiter
    → 如果 Permit 尚未转移给调用方但已经产生，则代为 release()
```

如果 `owned_permit` 已经有值，说明 Permit 所有权已经交给调用方，收尾方法不会再次释放。
这保证了 Permit 既不会泄漏，也不会被重复释放。

## 配置

生产配置由 `settings.py` 和 `Container` 注入：

- `tool_max_concurrent_executions`：所有运行共享的全局在途上限；
- `tool_max_concurrent_executions_per_run`：单运行在途上限；
- `tool_max_pending_calls`：全局等待队列上限；
- `tool_max_pending_calls_per_run`：单运行等待队列上限，不得大于 `tool_max_pending_calls`
  （由 `Settings` 与 `ToolAdmission` 两处同时校验，配置错误在启动期即报出）；
- `tool_admission_timeout_seconds`：一次准入等待上限。

Piece③只实现进程内准入。跨进程/多实例 fencing、资源身份和副作用冲突保护分别属于
后续生命周期切片。
