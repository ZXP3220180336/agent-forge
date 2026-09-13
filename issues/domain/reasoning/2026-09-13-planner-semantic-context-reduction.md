# Planner 三阶段载荷缺少语义上下文缩减（REASON-025）

状态：已修复。优先级：P1。发现来源：上下文预算跨策略闭环 T-02。范围：PlannerStrategy、PromptManager、ContextBudgetPort。

## 现象与影响

Planner 每步 ReAct 已支持消息轮次和 token 裁剪，但 plan、replan、summarize 三笔结构化
请求没有使用 `max_context_tokens`。重规划与汇总共用固定字符截断：单步先截 500 字符，
整体再取前 4000 字符。长任务可能从中间切断记录、偏向早期步骤，并丢失后部成功步骤、
依赖、失败原因或工具查询参数。Integration 最终请求预算闸只能拒绝过大的 wire payload，
无法替 Domain 决定应保留哪些业务事实。

## 根因与设计依据

`max_context_tokens` 只透传给内部 ReAct；PromptManager 的 Planner builder 没有预算和
token 计数入口。旧序列化器只保留工具名，也没有把 normalize 后的步骤依赖写入
`steps_executed`。

语义取舍与请求准入必须分层：[语义预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)
依据 LangGraph 消息缩减和 Semantic Kernel ChatHistory reducer，把候选上下文缩减视为
应用/领域语义职责；[请求准入 ADR](../../../adr/integration/llm/2026-09-06-request-context-budget.md)
继续让 Integration 计算 schema、工具和输出预留后的 provider 最终边界。Planner 的
目标、步骤因果和证据用途属于 Domain，因此没有把字段选择移入 ContextManager 或
LLMService，也没有增加一次 LLM 摘要调用。

## 修复方案与取舍

- 新增 prompts 包内私有 `_planner_payload.py`，使用注入的统一 token 计量形成只读分层
  投影；PromptManager 仅扣除固定模板开销并组装模板。
- plan 保留完整目标和全部工具身份，预算不足先缩减工具说明。
- replan/summarize 按原顺序保留全部步骤的 id、状态、描述和依赖；成功步骤保留结果及
  工具名/查询参数，失败步骤保留原因但不成为 supporting evidence。
- 省略带数量和 `reason="context_budget"` 标记；不覆盖原 plan、executed 或工具调用。
- 最小业务骨架仍超限时，Planner 在 SDK 调用前形成 `ContextWindowExceededError`，沿
  既有 Guard 保留 plan、步骤和此前 usage。单阶段零调用且不进行缩减重试。
- `steps_executed` 增加 `depends_on`；同时修复只有失败记录时以 `bool(executed)` 误判
  部分成功的分支，继续保持二维步骤成功和统一 plan 形状。

## 实施与验证

红测试先复现三个 builder 不接收预算、三个结构化阶段仍会发送已知超限请求。实现后
覆盖预算充足、工具目录缩减、失败根因/依赖、工具参数证据、后部关键步骤、输入只读、
三个阶段本地零调用、部分成果、usage 单计与失败记录成功口径。

专项回归 `tests/unit/test_prompts.py`、`test_planner.py`、`test_planner_agent.py`、
`test_reflection.py`、`test_react_strategy.py` 共 185 项通过。全量验证与对齐检查结果见
[完成记录](../../../docs/history/completed-work.md)。

## 教训与关联

按总字符取头部不是语义缩减：它既不知道步骤边界，也无法保证后部关键事实存在。多条
业务记录必须先定义每条最小骨架，再按统一层级降采样；成功证据与失败诊断也必须分开，
否则“保留工具调用”可能把失败尝试误交给报告作为证据。

关联：[Planner ADR](../../../adr/domain/reasoning/2026-09-05-planner-strategy.md) ·
[REASON-023](2026-09-13-reflection-semantic-context-reduction.md) ·
[REASON-024](2026-09-13-reflection-evidence-budget-underestimate.md)。
