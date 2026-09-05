# _common — reasoning 策略共享小工具

react / reflection / planner 三策略共用、不 import 任何策略的无状态纯函数与常量（策略间零环依赖）。
文件名带 `_` 前缀 = 层内私有共享（非对外导出）。

## 共享常量与小工具

- `_FINAL_ANSWER_TOOL = "final_answer"`：结构化最终答案工具名（层内私有常量）——react 主循环识别 /
  reflection 证据链剔除共用
- `merge_usage(*usages)`：usage dict 累加合并（prompt/completion/total；空入参跳过）——
  reflection 自查/修正阶段与 planner 全阶段共用
- `should_abort(cancel_event, start_time, max_execution_time) -> (bool, str)`：循环终止检查
  （用户取消 / 总时长超限）——reflection 自查循环与 planner 各阶段入口共用

使用方式：`from ._common import _FINAL_ANSWER_TOOL, merge_usage, should_abort, dispatch_error`。
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
