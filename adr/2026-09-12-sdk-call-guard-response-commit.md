# SDK 调用准入、响应接管与 Guard 提交边界

日期：2026-09-12。决策状态：已接受。实现状态：已验证。范围：Domain 推理策略、
Integration LLM 生命周期与非关键调用观测。

## Context

取消、绝对 deadline、成本和请求上下文预算同时承担两类职责：阻止下一次副作用，以及处理
一次调用等待期间发生的状态变化。若把调用后 Guard 放在响应接管之前，已产生的 content、
critique、plan、usage 或 reservation 责任会被丢弃；若完全省略调用后 Guard，又可能在运行
已经取消、超时或超成本后继续工具调用、refine、replan、fallback 或下一轮模型请求。

工业实现不会把这两类职责合并成一个无条件检查点：

- OpenAI Agents SDK 的 blocking input guardrail 在昂贵模型或工具启动前完成；output guardrail
  在输出产生后判定，同时保留已完成的工具历史与运行事实：
  <https://openai.github.io/openai-agents-python/guardrails/>。
- OpenAI Agents SDK 区分 immediate 与 after-turn 取消；after-turn 完成当前 turn、保存状态、
  统计 usage，再在下一 turn 前停止：
  <https://openai.github.io/openai-agents-python/ref/result/>。
- gRPC deadline 明确指出客户端可能得到 `DEADLINE_EXCEEDED`，即使服务端已经成功完成；
  远端事实与客户端终态必须分别处理：<https://grpc.io/blog/deadlines/>。
- Python asyncio 取消是协作式的，任务需要通过 `try/finally` 接管清理责任：
  <https://docs.python.org/3/library/asyncio-task.html#task-cancellation>。

备选方案包括：调用返回后立刻统一 Guard、全部业务解析后才 Guard、以及按调用生命周期和
Guard 类型分层处理。前两者分别会丢失事实或放大副作用窗口，因此采用第三种。

## Decision

### 1. 统一生命周期

每次真实 SDK 调用遵循：

```text
调用前准入 → reserve/等待 → SDK 前复查 → SDK 调用
→ 响应或失败事实接管 → 最小必要解码 → usage/资源结算
→ 有界 best-effort 调用观测 → 适用的调用后 Guard → 同步业务提交或终止
```

调用前检查取消、deadline、成本和适用预算；Integration 对最终 payload 执行请求上下文准入。
任何可能等待的 reserve 后都要在 SDK 启动前复查。此时终止可 `cancel()` 尚未使用的
reservation，且不得启动请求。

SDK task 一旦调度，请求是否到达 Provider 即可能未知。Owner 必须接管返回值、迟回 stream、
异常与 reservation；普通传输异常也不得断言请求未发。未知实际 usage 以 `settle(None)`
保守关闭本地预留责任，只有 SDK 启动前才允许全额退款。

调用后 Guard 前允许的工作仅限于响应所有权接管、必要解码、usage 提取、资源结算和不触发
外部业务副作用的策略判定。工具调用、下一轮 LLM、refine、replan、fallback 和普通 summary
必须在 Guard 放行后开始。

### 2. Guard 类型不采用同一提交语义

| Guard | 调用前 | 调用后 |
| --- | --- | --- |
| 成本 | 超限即拒绝下一笔付费调用 | 记录本轮真实费用并关闭下一调用准入；不能撤销本轮成果 |
| graceful / after-turn cancel | 已置位则拒绝调用 | 接管并结算当前原子 turn，停止下一副作用；策略允许时可提交该 turn 的有效终态 |
| strict deadline | 已到期则拒绝调用 | 迟到结果不得标为按时成功；以 TIMEOUT/部分成果终态提交，并保留有效事实 |
| 请求上下文超限 | Provider 前拒绝 | 保留此前成果与 usage；只有 payload 经真实语义缩减后才能重新准入 |

Guard 优先级仍由 reasoning `_common` 的现有契约维护；优先级只选择终止原因，不允许跳过事实
接管和结算。

### 3. Strategy 适用边界

- ReAct 保持调用后先接管可见结果与 usage、再 Guard、再解释工具或终态的顺序。
- Planner 的 STOP 是已选终态，不得进入 ReAct fallback；上下文超限在尚无语义缩减时终止。
- Reflection 保留 REASON-020 的成本和 after-turn 取消语义：自查已经形成 `ok` 或已经达到
  修正上限且没有下一笔调用时，不因成本或迟到的 graceful cancel 改判合格稿。strict
  deadline 不适用此豁免；迟到稿、critique 与 usage 保留，但终态为 TIMEOUT/部分成果。
  自查未返回可用 critique 时，先识别 cancel/deadline/context Guard，再决定是否按
  `CRITIQUE_FAILED` 降级。

### 4. 提交与观测

必要结算是提交前责任；结算异常保持可见。`llm_call` 记录的是 SDK attempt 事实，而非
Strategy 的最终成功，因此可在最终 Guard 前完成，但必须有界且 best effort。日志异常或自身
超时不得覆盖成功、原始传输错误、cancel、deadline 或结算错误。非流式最终 Guard 与同步返回
之间不得再存在可阻塞业务 await。流式已发送 chunk 是已提交事实；EOF 后的收尾不得触发整流、
续接或重发。

## Consequences

该顺序同时满足 G0-2/G0-3/G0-4/G0-6/G0-7，并使“结果可用性”和“运行终止原因”能够同时
表达。代价是 Reflection 的 deadline 与成本/取消需要分支处理，不能用一个无条件 Guard
检查点代替。

本决策不实现 Reflection/Planner 的语义上下文缩减；该能力仍由 T-01/T-02 单独推进。也不在
本轮重设计远端请求的幂等键或核对机制。

## 实现与验证

- Integration：create 调度后的普通异常使用 `settle(None)`；LLM 调用日志以有界
  best-effort 方式记录；非流式最终 Guard 后同步提交。
- Domain：Reflection 区分 strict deadline 与成本/after-turn 取消；Planner 的 STOP
  不再启动 fallback，上下文超限不进入同语义重试；ReAct 经复核无需修改。
- 回归证据：`test_llm_service.py`、`test_llm_request_budget.py`、`test_logger.py`、
  `test_reflection.py`、`test_planner.py` 覆盖上述边界；完整命令结果记录在
  [当前计划](../docs/todo.md)。

## 关联记录

- [治理目标](2026-09-12-single-source-governance.md)
- [REASON-020 Reflection 自查早退](../issues/domain/reasoning/2026-09-11-reflection-guard-checkpoint.md)
- [LLM-044 执行控制](../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)
- [LLM-046 EOF 后完成态](../issues/integration/llm/2026-09-09-continuation-finish-guard.md)
- [LLM-048 create 启动后普通异常结算](../issues/integration/llm/2026-09-12-create-started-ordinary-error-settlement.md)
- [LLM-049 非关键 LLM 观测隔离](../issues/integration/llm/2026-09-12-llm-observation-overrides-terminal.md)
- [REASON-021 Planner STOP 终态](../issues/domain/reasoning/2026-09-12-planner-stop-starts-fallback.md)
- [REASON-022 结构化 Guard 终态保留](../issues/domain/reasoning/2026-09-12-structured-guard-terminal-loss.md)
