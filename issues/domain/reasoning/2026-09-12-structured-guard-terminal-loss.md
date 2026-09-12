# 结构化阶段丢失 strict deadline 与上下文 Guard 终态（REASON-022）

状态：已修复。优先级：P2。发现来源：R1/R4/R6 复审。范围：Reflection、Planner。

## 现象与影响

Reflection 自查成功但迟于绝对 deadline 时按 REASON-020 提交正常成功；自查无结果时又在
Guard 复查前归为普通自查失败。Reflection/Planner 的宽泛 `except AppError` 还会把
`ContextWindowExceededError` 改写成 `CRITIQUE_FAILED`/`PLAN_FAILED`，Planner 可能对未缩减
语义的请求启动 ReAct fallback。

## 根因与修复

旧实现把成本/after-turn 取消的完成语义扩展到了 strict deadline，并让普通失败动作槽承担
终结性请求 Guard。按照 [ADR-003](../../../adr/2026-09-12-sdk-call-guard-response-commit.md)：

- 保留 REASON-020 的成本和 after-turn 取消行为；
- 自查 `ok` 或达上限时单独判 strict deadline，迟到则保留稿、critique、usage 并以 TIMEOUT
  部分成果收尾；
- 自查无结果时先恢复 cancel/deadline/context 终态，再判普通 CRITIQUE_FAILED；
- 结构化辅助显式返回 context error 与 usage，Planner 在没有语义缩减时关闭 fallback；
- replan/summarize 同样把上下文超限送入共享 Guard，并保留已有步骤成果。

本修复不实现 T-01/T-02 的语义缩减。新增 Reflection 迟到成功、after-turn cancel、上下文
超限和 Planner 上下文超限零 fallback 测试。完整验证见
[完成记录](../../../docs/history/completed-work.md)。
