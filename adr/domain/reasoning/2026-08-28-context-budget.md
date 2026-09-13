# 语义上下文预算（ContextBudgetPort + ContextManager）

> 状态：✅ 已采纳
> 日期：2026-08-28 ｜ 层级：domain + application

## Context

- 对标核心必备 #10 缺失：ReAct 循环 messages 无限增长，长任务可击穿上下文窗口。
- 工业级参照：LangGraph 在模型节点前对图状态内的消息执行裁剪、删除或摘要；Semantic Kernel 以 `IChatHistoryReducer` 管理 `ChatHistory` 的截断与摘要。这类能力决定会话语义的保留与压缩，而非 provider 请求的可发送性。[LangGraph](https://docs.langchain.com/oss/javascript/langgraph/add-memory) · [Semantic Kernel](https://learn.microsoft.com/en-us/semantic-kernel/concepts/ai-services/chat-completion/chat-history)
- 分层调研（DDD）：会话语义预算是**横切能力**（所有 Agent 模式共享），应统一归属而非 ReAct 专属；`ContextManager` 是其自然归属（复用 `TokenCounter` 与会话上下文职责）。
- 评审修正：轮次预算不保证 token 不超限（OpenAI cookbook 对 huge tool payloads 的已知警告）→ **双层护栏**。

## Decision

1. `ContextManager` 统一承担**候选消息的语义预算**（application 层），经领域端口 `ContextBudgetPort` 注入 Agent——所有模式（ReAct/Planner/Reflection）共享，ReAct 不实现算法。依赖方向：domain 定义端口 → application 实现 → container 组装注入。
2. **语义缩减护栏**：轮次滑动窗口（保留 system/user + 最近 N 轮 assistant/tool 配对原子，模型看不到孤立 tool 结果）+ token 策略上限（超限逐轮丢最旧，复用 TokenCounter，补 tool_calls/reasoning_content 低估修正）。该护栏在模型调用前形成候选 messages。
3. 最终 provider 请求的窗口准入、工具定义与结构化 schema 计数、输出预留，归 Integration `LLMService` 的请求预算闸，见 [request-context-budget](../../integration/llm/2026-09-06-request-context-budget.md)。`ContextManager` 不依赖供应商协议，也不序列化最终 wire payload。
4. **配置**：`agent_max_context_rounds=8`（轮次）+ 复用 `max_context_tokens`（token 策略上限）。`AgentContext` 默认 None（策略层向后兼容），生产由装配根注入。
5. **session 存储下沉基础设施**记为升级路径（非本次）：工业级惯例为「会话用例在应用层、存储实现下沉基础设施（仓储接口）」，当前 session_manager 混合两者，后续可拆。
6. Reflection 的阶段性单条载荷使用同一端口的 `count_tokens` 计量，但字段选择留在 Domain：critique 按 evidence 60% / draft 40%，refine 按 evidence 45% / draft 35% / issues 20% 初分配并回流余量。缩减视图保留被引用证据、结论、量测/时间锚点和 unresolved issues，并显式标记省略；原始证据、最近完整稿与 critique 不被覆盖。最终 wire payload 仍由 Integration 请求闸裁决。
7. Planner 的 plan/replan/summarize 阶段性载荷同样使用 `count_tokens`，字段选择留在 Domain：目标始终完整保留；规划先缩减工具说明并保留全部工具身份；重规划和汇总按原执行顺序保留全部步骤的 id、状态、描述与依赖，成功步骤保留结果及工具名/查询参数，失败步骤保留原因但不作为结论证据。缩减采用分层投影并显式记录省略量，不覆盖原始 plan、executed 或工具记录。最小业务骨架仍超限时在 SDK 调用前形成上下文 Guard，单阶段不发请求、不做缩减重试。

## Consequences

- ✅ 核心必备 13 项全部落地；上下文预算成为所有 Agent 模式共享的横切能力。
- ✅ 轮次保配对原子性；候选消息 token 上限保证会话状态有界。
- ✅ 语义取舍与 provider 请求准入分属单向依赖的两层，避免 `LLMService` 反向依赖 Application。
- ⚠️ 被 trim 的早期轮信息丢失（滑动窗口固有取舍，与 LangChain trim 一致）；token 估算保守（补低估，方向安全）。
- ✅ Reflection 自查/修正已复用统一 token 计量并实施字段级语义缩减；实现与测试见 [REASON-023](../../../issues/domain/reasoning/2026-09-13-reflection-semantic-context-reduction.md)。
- ✅ Planner 三个结构化入口已实施分层语义投影；实现与测试见 [REASON-025](../../../issues/domain/reasoning/2026-09-13-planner-semantic-context-reduction.md)。所有策略的最终请求继续由请求预算闸校验。
