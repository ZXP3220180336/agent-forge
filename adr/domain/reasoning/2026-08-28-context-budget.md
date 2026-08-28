# 上下文预算管理（ContextBudgetPort + context_manager 统一）

> 日期：2026-08-28 ｜ 层级：domain + application

## Context

- 对标核心必备 #10 缺失：ReAct 循环 messages 无限增长，长任务可击穿上下文窗口。
- 工业界机制调研：trimming（LangChain trim_messages / OpenAI last-N turns）、summarization（compaction）、单条截断。选 **trimming**——工具密集短任务适用；摘要压缩工具原始记录会破坏证据链（产品核心）。
- 分层调研（DDD）：上下文预算管理是**横切能力**（所有 Agent 模式共享），应统一归属而非 ReAct 专属；context_manager 是「上下文管理」的自然归属（复用其 TokenCounter + 上下文职责）。
- 评审修正：轮次预算不保证 token 不超限（OpenAI cookbook 对 huge tool payloads 的已知警告）→ **双层护栏**。

## Decision

1. **context_manager 统一承担**（application 层），经领域端口 `ContextBudgetPort` 注入 Agent——所有模式（ReAct/Planner/Reflection）共享，ReAct 不实现算法。依赖方向：domain 定义端口 → application 实现 → container 组装注入。
2. **双层护栏**：轮次滑动窗口（保留 system/user + 最近 N 轮 assistant/tool 配对原子，模型看不到孤立 tool 结果）+ token 预算硬上限（超限逐轮丢最旧，复用 TokenCounter，补 tool_calls/reasoning_content 低估修正）。模型调用前作为 gatekeeper。
3. **配置**：`agent_max_context_rounds=8`（轮次）+ 复用 `max_context_tokens`（token 硬上限）。`AgentContext` 默认 None（策略层向后兼容），生产由装配根注入。
4. **session 存储下沉基础设施**记为升级路径（非本次）：工业级惯例为「会话用例在应用层、存储实现下沉基础设施（仓储接口）」，当前 session_manager 混合两者，后续可拆。

## Consequences

- ✅ 核心必备 13 项全部落地；上下文预算成为所有 Agent 模式共享的横切能力。
- ✅ 轮次保配对原子性；token 硬上限保证总量有界。
- ⚠️ 被 trim 的早期轮信息丢失（滑动窗口固有取舍，与 LangChain trim 一致）；token 估算保守（补低估，方向安全）。
- 📌 其他模式（Planner / Reflection）复用同一端口即可接入。
