# Reflection 阶段载荷缺少语义缩减（REASON-023）

状态：已修复。优先级：P1。发现来源：上下文预算跨策略闭环 T-01。范围：Reflection、PromptManager、ContextBudgetPort。

## 现象与影响

Reflection 的 ReAct 收集阶段会裁剪多轮消息，但自查和修正把 `evidence`、`draft`、
`issues` 直接拼成一条 user 消息。旧证据序列化还会无标记截取前 4000 字符，因此可能
丢掉当前稿实际引用的后部证据；稿件与审查意见没有语义预算。请求最终只能由 Integration
预算闸拒绝，无法在调用前保留关键字段并主动缩减。

复核还发现：已经取得 critique 后，修正调用前 Guard 或修正请求上下文拒绝的收尾没有
携带 critique，导致已发生且有审计意义的事实丢失，不符合 G0-4。

## 方案与取舍

沿用[语义预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)及其
LangGraph、Semantic Kernel 工业参照：`ContextManager` 经 `ContextBudgetPort.count_tokens`
提供统一计量，字段优先级由 Reflection 所属的 Domain prompt 边界决定，Integration 继续
负责包含 schema 和输出预留的最终请求准入。

- critique 动态预算按 evidence 60% / draft 40% 初分配；refine 按 evidence 45% /
  draft 35% / issues 20% 初分配，未使用额度按字段声明顺序回流。
- evidence 优先保留当前稿引用的记录，再保留最近记录；稳定编号、工具、参数、成功状态、
  错误码及数值/时间锚点优先于长结果正文。
- draft 优先保留 summary、conclusions、supporting_evidence、confidence 与
  explicit_abstention；issues 将 critical 排在 minor 前，并优先较新的 minor。
- 所有缩减只生成 prompt 视图，使用带数量的 `<omitted .../>` 标记；不修改原始
  `tool_calls`、draft、current 或 critique。
- 本地最小提示骨架仍超过语义预算时零调用并采用最近完整稿；预缩减后若最终预算闸仍拒绝，
  不进行缩减重试。这样单阶段最多一次真实请求。

没有新增摘要 LLM 调用，也没有修改 `LLMGateway` 请求契约。字符或 token 字段选择不承担
provider wire payload 的精确准入，后者仍由 Integration 独立裁决。

## 实施与验证

实现覆盖 `app/domain/prompts/manager.py`、`app/domain/reasoning/reflection.py` 和
`app/domain/ports/context_budget.py`。测试锁定长 evidence/draft/issues、原对象不变、
省略计数、被引用后部证据、数值/时间锚点、零调用最小骨架、最终拒绝单调用，以及 Guard
终止保留完整 critique。核心 Prompt/Reflection 测试 46 项、相关 ContextManager/桥接测试
合计 64 项通过；最终全量 975 项通过，唯一告警为既存的 Starlette/httpx 弃用提示；
`scripts.verify_alignment` 与 `git diff --check` 通过。完整交接见
[完成记录](../../../docs/history/completed-work.md)。

## 教训与关联

语义缩减视图不是业务事实对象；缩减只能影响下一次请求的可见输入，不能覆盖已经接管的
证据、稿件或审查意见。相关：[REASON-022](2026-09-12-structured-guard-terminal-loss.md) ·
[请求准入 ADR](../../../adr/integration/llm/2026-09-06-request-context-budget.md)。
