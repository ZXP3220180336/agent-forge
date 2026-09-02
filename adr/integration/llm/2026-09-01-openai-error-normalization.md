# 集成层 openai 异常归一：LLMAPIError 入 AppError 树

> **状态**：✅ 已采纳
> **决策日期**：2026-09-01
> **涉及模块**：`app/integration/llm/errors.py` · `app/integration/llm/llm_service.py` · `app/integration/llm/structured.py` · `app/shared/exceptions.py`
> **关联文档**：[error.md](../../../docs/integration_doc/llm_doc/error.md) · [LLM 层说明](../../../docs/integration_doc/llm_doc/llm.md)（LLM-ADR-013）

---

## Context

- REASON-010 遗留缺口：`app/integration/llm/llm_service.py` 的 `generate` 对 `NON_RETRYABLE` 错误**直接 raise openai 原始异常**（`APIStatusError` 系列），领域层 `except AppError` 兜不住 → Reflection 自查/修正阶段遇 API key 过期（401）等崩溃，违背「不抛错降级」承诺。
- **工业级参照**（Hexagonal Adapter / OpenAI SDK / LangGraph）：基础设施异常在 **adapter 边界**翻译为领域异常、`raise DomainError from exc` 链原始异常、领域异常携带 code/status 由外层映射 HTTP（[Translate Infrastructure Errors at the Adapter](https://dev.to/gabrielanhaia/translate-infrastructure-errors-at-the-adapter-not-in-your-domain-5224)）；openai SDK 将 401/400/403 归「永久错误」不重试（[OpenAI SDK 错误处理](https://deepwiki.com/openai/openai-python/3.4-error-handling-and-retry-logic)）；LangGraph 对 401 不重试、编程错误冒泡 fail-fast（[LangGraph Fault Tolerance](https://langchain-5e9cc07a.mintlify.app/oss/javascript/langgraph/fault-tolerance)）。

## Decision

1. **新增 `LLMAPIError(NonRetryableError)`**（`app/shared/exceptions.py`，`code=AppErrorCode.LLM_API_ERROR`），携带 `status_code`；包装时 `raise LLMAPIError(...) from e` 保留原始 openai 异常。
2. **归一位置 = `llm_service.generate` except 边界**（非 retry 层）：retry/classify/熔断继续操作原始 openai 异常，归一发生在其后。**落地载体 = `app/integration/llm/errors.py`**：新增 `normalize_transport_error(exc)`（复用 openai 类型知识）包装 `APIStatusError`（带 status_code）+ `_NON_RETRYABLE_EXC`（status_code=None）；其余返回 None（429/5xx/超时走 return None、非 openai 异常原样透传）。`generate` 下游统一决策由 `decide_downstream_error(exc) -> DownstreamDecision` 承担（归一上抛 / 原样上抛 / 降级三选一），llm_service / structured 只消费结果。
3. **不复用 `UnauthorizedError/ForbiddenError/NotFoundError`**（BusinessError，承载 API 会话认证语义）——LLM provider 认证错误与用户会话授权语义不同，混用会误导领域 handler。
4. **`async_generate` 流式路径不归一**：它已把异常转 `StreamResult.error` 字符串 + error 事件，无异常逃逸，不构成 AppError 覆盖缺口。
5. **status_code 保留是硬约束**：`is_unsupported_response_format_error`（`errors.py`，structured 消费）依赖 `status_code==400` + message 关键词判定「response_format 不支持」降级，不保留该降级链即断裂。
6. **API 边界映射**：`error_handler._CODE_TO_STATUS` 加 `LLM_API_ERROR → 502`（上游 provider 故障语义），**不映射 401/403**——避免误导客户端以为自身会话失效。

## Consequences

- ✅ 领域层 `except AppError` 统一兜住集成层透出的不可恢复错误（Reflection 自查/修正降级采用最近稿，REASON-010 闭环）。
- ✅ `raise ... from e` 保留原始 openai 异常（诊断经 `__cause__`）；编程错误（非 openai）仍冒泡 fail-fast。
- ✅ 熔断/重试/fallback 语义零扰动（归一在 retry 之后）；`response_format` 400 降级链存活（status_code 保留）。
- ⚠️ LLMAPIError 的 message 源自 openai body（可能含 URL/内部细节）——沿用既有 `[:200]`（日志）/`[:500]`（result.error）截断口径，不扩大。
- 📌 升级路径：若出现「LLM 401 与 403 差异化处理」真实需求（如给良率工程师区分「key 过期」与「权限不足」），拆具名子类（`LLMUnauthorizedError`/`LLMForbiddenError`），当前 status_code 已够差异化，不预先拆分。
