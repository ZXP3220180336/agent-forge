# LLM 层日志：全局 JSON 结构化 + llm_call 业务事件

> **状态**：✅ 已采纳
> **决策日期**：2026-08-04（全局日志框架升级，LLMLogger 移除并入）
> **涉及模块**：`app/platform/observability/logger.py`（全局日志框架）· `app/integration/llm/`（`llm_call` 业务事件）
> **关联文档**：[llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [logging.md](../../../docs/platform_doc/observability/logging.md)

---

## Context

- LLM 调用需记录元数据（模型、Token、耗时、是否成功）供可观测性/成本审计。
- 早期 `LLMLogger`（`app/integration/llm/logger.py`）是 LLM 层私有实现。
- 日志格式与归属有两种选择：JSON 结构化（全局）vs 纯文本（私有）；消费端（ELK/Datadog/Graylog）需可解析。

## Decision

**LLM 层日志走全局 JSON 结构化日志框架 + `llm_call` 业务事件；LLMLogger 私有实现移除（2026-08-04），职责并入全局框架。**

1. **全局统一**：日志是横切关注点——所有模块（LLM/服务/Agent/API）走同一双 handler（文件 JSON + 控制台人类可读），消费端无需分模块解析。
2. **业务事件 `llm_call`**：事件名即 `message`，字段（model / messages_count / temperature / has_tools / stream / success / error / duration / prompt_tokens / completion_tokens / total_tokens / finish_reason）经 extra 注入 JSON；可按事件/字段查询。
3. **异步写入**：`log_event_async` → `asyncio.to_thread`，不阻塞 LLM 调用主流程。
4. **不记录敏感信息**：只记录 messages 数量、不记录内容——模型输出不完整落盘安全基线。
5. **统一填充**：`fill_llm_event_fields`（统一填充 + 记录，被 generate / 整流循环复用），避免各调用点手工拼字段。

## Consequences

- **正面**：全局统一消费（ELK/Datadog/Graylog 单套解析）；异步写入不阻塞主流程；结构化可检索（任意字段过滤）；日志不落敏感内容。
- **负面**：JSON 文件可读性低于纯文本（视觉噪音，控制台 handler 补偿）；双 handler（文件/控制台）需维护格式一致性；业务事件字段扩展需同步全局框架与调用点。
