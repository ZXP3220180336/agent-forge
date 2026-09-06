# _common — reasoning 策略共享小工具

react / reflection / planner 三策略共用、不 import 任何策略的无状态纯函数（策略间零环依赖）。
文件名带 `_` 前缀 = 层内私有共享（非对外导出）。

## 共享小工具

- `merge_usage(*usages)`：usage dict 累加合并（prompt/completion/total；空入参跳过）——
  reflection 自查/修正阶段与 planner 全阶段共用
- `guard_exceeded(cancel_event, start_time, max_execution_time, cost_limiter, running_usage) -> (str, str)`：
  阶段/付费调用前护栏——终止（取消/超时）或成本超限（cost_limiter.check(running_usage)）检查合一，
  返回双空原因；running_usage 由调用方按策略累计口径现算（planner / reflection 各传各自累计 usage）。
  **成本分工**：本函数的 cost 检查是「阶段准入闸」（结构化调用 / 阶段边界发起前拦）；react 子跑**内部每轮**
  的累计成本检查经 `ReActStrategy.execute(baseline_usage)` 贯通（跨阶段复用方如 planner 步骤子跑注入调用方
  累计用量）——两层各管各粒度、非重复（见 [react.md](react.md) 成本上限节 / [planner.md](planner.md) 设计要点 #2）

使用方式：`from ._common import dispatch_error, guard_exceeded, merge_usage`。
策略不再各持本地定义；error_handlers 等生命周期仍由各策略自行管理。

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
