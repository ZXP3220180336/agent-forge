# Planner 已选择 STOP 后仍启动 ReAct fallback（REASON-021）

状态：已修复。优先级：P1。发现来源：R6/G0-2 复审。范围：`reasoning/planner.py`。

## 现象、根因与修复

规划调用失败且 `PLAN_FAILED` handler 返回 STOP 时，代码仍运行 `_run_react`，仅在最终结果把
`success` 改为 false；重规划失败后的 STOP 也未从 `_replan_loop` 返回，已有成功步骤时还会
启动付费汇总。STOP 已选择终态，这些后续调用都是新的业务副作用。根因是动作只参与结果字段，
没有成为控制流分支。

修复在规划 fallback 前直接构造失败终态并返回，并让重规划循环把 STOP 动作显式交还主循环，
以已完成步骤收尾且不再汇总。CONTINUE 仍执行既有降级，RAISE 仍由 `dispatch_error` 上抛。
测试分别锁定 `react_calls == 0` 与 STOP 后 `structured_calls` 不再增长。完整验证见
[完成记录](../../../docs/history/completed-work.md)。

工业参照采用 OpenAI Agents SDK 的终止后停止后续工作语义；项目正式约束为 G0-2/R6。
关联：[ADR-003](../../../adr/2026-09-12-sdk-call-guard-response-commit.md)。
