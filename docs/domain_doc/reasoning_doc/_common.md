# _common — reasoning 策略共享小工具

react / reflection / planner 三策略共用、不 import 任何策略的无状态纯函数（策略间零环依赖）。
文件名带 `_` 前缀 = 层内私有共享（非对外导出）。

> **更新日期**：2026-09-10

## 共享小工具

- `merge_usage(*usages)`：usage dict 累加合并（prompt/completion/total；空入参跳过）——
  reflection 自查/修正阶段与 planner 全阶段共用
- `GuardResult`：不可变、带 slots 的类型化护栏结果，字段为 `kind: AgentErrorKind`、`message`、
  可选 `cost_usd`；`None` 表示允许继续。
- `evaluate_guard(*, cancel_event, deadline, cost_limiter, running_usage, cancelled=False,
  deadline_exceeded=False, context_error=None) -> GuardResult | None`：使用 monotonic 绝对
  deadline，并固定按 `CANCELLED > TIMEOUT > COST_EXCEEDED > CONTEXT_EXCEEDED` 判定。
  `cancelled` / `deadline_exceeded` 接收 LLM Facade 已识别的类型化信号；`context_error` 只由捕获
  `ContextWindowExceededError` 的策略传入。成本检查仅在更高优先级信号未命中时执行。

`running_usage` 由调用方按本策略累计口径现算，函数不修改 usage。**付费调用前准入**是三个策略
的共同规则；**调用成功并归账后复查**由 ReAct / Planner 在每笔付费调用后执行，Reflection 只挂在
**修正**调用上——自查返回后不再有付费动作，复查只会把已合格的稿改判为降级。ReAct 子跑通过
`baseline_usage` 把 Planner 已累计用量带入每轮判定。

使用方式：`from ._common import GuardResult, dispatch_error, evaluate_guard, merge_usage`。
策略不再各持本地定义；error_handlers 等生命周期仍由各策略自行管理。

## 测试

`tests/unit/test_reasoning_common.py` 直接锁定结果不可变性、绝对 deadline 边界以及四类护栏优先级；
三策略测试负责验证判定结果对应的降级内容、usage 归并和调用次数。

## 错误分发统一入口（dispatch_error）

`dispatch_error(error_handlers, kind, message, iteration=0) -> AgentErrorAction`：
RAISE 决策抛 `AgentRunError`，否则返回 action。

- 收敛三策略各自实现的「`registry.dispatch` + `AgentErrorContext` 构造 + RAISE 抛」机械段
- `error_handlers` 由调用方传入——**本模块不维护其生命周期**（无默认注册表单例）：各策略在
  构造时解析并持有（`error_handlers or ErrorHandlerRegistry()`），dispatch 时传入
- 使用方：`react._dispatch`（唯一入口，委托本函数）；reflection / planner 的结构化调用失败
  （`CRITIQUE_FAILED` / `PLAN_FAILED`）直接调用

设计取向：仅收无状态纯函数、不建全局可变状态——策略间隔离性（各实例独立 registry / 可注入
不同 handler）由策略实例保证，本模块不引入共享生命周期。
