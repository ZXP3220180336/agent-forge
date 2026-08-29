# 异常体系完整优化（error_handler 边界 + 命名修正 + API 收敛）

> 日期：2026-08-28 ｜ 层级：shared + api + domain

## Context

- 用户观察异常体系"分布各处"（`retry.py` ErrorCategory / `tool_gateway.py` ErrorCode / `error_handling.py` AgentErrorKind / `exceptions.py` AppError 树），要求调查工业级后优化。
- **工业级调研**（OpenAI SDK / LangChain / Spring AI / SMOLagents / FastAPI，源码级）：**分层分类 + 共享宽松统一基类是正确形态**——四套分类（传输可重试性 / 工具系统码 / Agent 编排 / 对外业务码）的消费者、生命周期、变更频率不同，**不应合并**。本项目现状结构正确。
- **真实缺口**：`AppError` 树 + `AppErrorCode` 无消费方——`app/api/middleware/error_handler.py` 是空文件，API 层全手写 `HTTPException`（deps/session/chat 共 8 处）；`AgentError` 命名与 SMOL 家族基类语义冲突。

## Decision

1. **保持四套分类正交不合并**（调研结论）；本次只接边界 + 修正命名 + API 收敛。
2. **`error_handler.py` 实现**（Phase D error_handler）：AppError → HTTP 状态 + 统一 `{code, message, details}` 信封；业务码与 HTTP 状态解耦（`_CODE_TO_STATUS` 映射表）；`main.py` 调用 `register_error_handlers(app)`。
3. **`exceptions.py` 补 API 边界异常**：`AppErrorCode` 加 `UNAUTHORIZED` / `FORBIDDEN` / `NOT_FOUND`；新增 `UnauthorizedError` / `ForbiddenError` / `NotFoundError`（BusinessError 子类）。
4. **API 层收敛**：deps / session / chat 的 8 处 `HTTPException` → AppError 子类（统一走 error_handler 信封）。
5. **命名修正**：`AgentError` → `AgentRunError`（携带 kind 的叶子类，与 SMOL「家族基类」概念解耦；`AgentErrorKind` 保留）。

## Consequences

- ✅ `AppError` 树有边界消费方（error_handler），异常体系完整闭环；API 响应统一 `{code, message}` 信封
- ✅ 四套分类正交保留（工业级形态）；命名解耦；`except AppError` 批量捕获仍生效
- ⚠️ API 层不再抛 `HTTPException`（全走 AppError）；未收敛处 FastAPI 默认处理，可后续统一
- 📌 升级路径：`classify_error` 识别 `AppError` 树（可重试判定收敛单一事实源）；`AgentRunError` 的 kind → 对外业务码映射（产品需要时）
