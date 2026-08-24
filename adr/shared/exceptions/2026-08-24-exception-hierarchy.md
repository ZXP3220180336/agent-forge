# 统一异常体系：异常收敛到共享内核（可恢复性编码进类型 + 错误码）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-24
> **涉及模块**：`app/shared/exceptions.py` · `app/integration/llm/retry.py` · `app/integration/llm/structured.py` · `app/integration/tools/security.py` · `app/integration/tools/validator.py`
> **关联文档**：[error_handling.md](../../../docs/shared_doc/error_handling.md) · [architecture.md](../../../docs/architecture.md)

---

## Context

- 架构文档共享内核目标：`exceptions.py（异常体系 → 错误码）｜ 统一异常与错误码`，此前为 0 字节空文件。
- 现状问题：全项目 7 个自定义异常平级散落——`CircuitBreakerOpenError`（retry.py）、`StructuredExtractionError`/`TruncationError`/`RefusalError`/`ToolCallError`（structured.py）、`SSRFError`（security.py）、`ParameterValidationError`（validator.py，继承 ValueError）——无统一基类、无错误码，违反「一个事实一个家」。
- `error_handling.md` 已定义完整异常哲学（可恢复/不可恢复/业务边界三类 + 分层处理），但未落成代码。

## Decision

**收敛到 `app/shared/exceptions.py`：统一异常树 + `AppErrorCode` 错误码，集成层原模块 re-export。**

1. **树结构按「可恢复性 + 业务边界」分，不按模块分**：`AppError`（根）下 `NonRetryableError`（不可恢复：向上抛）与 `BusinessError`（业务边界：具名短路）两支，承载现有 7 异常。**不建空的 `RetryableError` 分支**——可恢复异常由可靠性层用 `ErrorCategory` 分类消化（不包装具名异常），空分支是堆砌（YAGNI）。
2. **`AppError` 契约**：`code: AppErrorCode` 类属性 + `__init__(message="")`，与 `Exception` 调用兼容；根默认 `INTERNAL`，子类覆盖。
3. **`ParameterValidationError(NonRetryableError, ValueError)` 多重继承**：既入统一树（`except AppError` 可捕获），又保留 `except ValueError` 语义。
4. **错误码最小集**：只定义当前异常实际关联的码 + `INTERNAL` 兜底；HTTP 语义码（AUTHENTICATION/NOT_FOUND/RATE_LIMITED）留到 Phase D error_handler 落地时再加。与 `ErrorCategory`（LLM 传输分类）、工具层 `ErrorCode`（工具执行系统码）正交，三者互不替代。
5. **re-export 收敛**：retry/structured/security/validator 删本地异常定义，改 `from app.shared.exceptions import ...`——raise 点与测试 import 路径不变，零断裂。
6. **工业级参照**：LiteLLM / openai SDK 把 provider 异常归一化为具名异常向上抛；FastAPI 生态将异常映射错误码供中间件统一响应。

## Consequences

- **正面**：异常单一事实源（收敛散落）；可恢复性语义编码进类型（`except NonRetryableError`/`BusinessError` 批量处理）；为 Phase D error_handler 提供错误码前置契约；测试零断裂（re-export 保路径）。
- **负面**：异常定义从集成层移到共享内核，集成层模块 import 路径变更（内部，无外部 API 影响）；错误码当前仅 7 个，error_handler 需补充 HTTP 语义码。
