# Reflection 证据预算低估导致充足预算仍省略（REASON-024）

状态：已修复。优先级：P1。发现来源：PromptManager 职责治理的行为刻画测试。范围：Domain prompts。

## 现象与影响

为预算充足的单条良率证据构造 critique 提示词时，结果仍只包含
`<omitted section="evidence" .../>`。这会在没有上下文压力时丢失本可完整送审的证据，降低
RCA 报告的可验证性；`final_answer` 后的真实证据也因此无法出现在提示词中。

可复现证据是新增的两个 PromptManager 行为测试：修复前 `tests/unit/test_prompts.py`
出现 2 项失败，均观察到充足预算下证据被错误省略。

## 根因与方案

预算分配使用 `_raw_evidence` 估算需求，但实际输出由 `_evidence_line` 生成，后者还会采用
紧凑参数格式并追加量测/时间锚点。分配器把证据额度截到偏小估算值；即使总预算有大量
剩余，也因估算认为证据已经满足而不再回流，最终序列化结果超过局部额度并被省略。

沿用[语义预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)记录的
LangGraph、Semantic Kernel reducer-before-model 工业边界，本项目采用“按实际候选表示计量”：
预算需求与最终证据行共用 `_evidence_line`，保证用于分配的文本和准备发送的文本同形。
不通过无条件放大额度掩盖误差，也不移除 Integration 的最终请求准入。

治理时同时把 Reflection 动态载荷策略迁入 `app/domain/prompts/_reflection_payload.py`；
`PromptManager` 继续作为公开 Facade。该结构调整不改变端口、公开签名或业务优先级。

## 实施与验证

新增测试覆盖预算充足时无省略、`final_answer` 排除后保留原来源编号，以及极小预算仍产生
省略标记。修复后 Prompt 测试 11 项通过；模块提取后 Prompt/Reflection 相关测试 49 项通过；
最终全量 978 项通过，唯一告警为既存 Starlette/httpx 弃用提示。`scripts.verify_alignment`
与 `git diff --check` 通过；项目环境未安装 Ruff，未把该命令记录为通过。

## 教训与关联

预算估算必须使用与最终候选载荷相同的序列化表示；仅依据原始对象或近似文本计量，会在
局部配额回流算法中形成假“已满足”。关联 [REASON-023](2026-09-13-reflection-semantic-context-reduction.md)
及[提示词模块](../../../docs/domain_doc/prompts_doc/prompts.md)。
