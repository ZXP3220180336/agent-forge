# 通用类型 / 标识：共享内核集中定义（最小集）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-24
> **涉及模块**：`app/shared/types.py` · `app/application/session/session_manager.py` · `app/application/context/context_manager.py` · `app/domain/agent/base.py` · `app/domain/ports/llm_gateway.py` · `app/domain/ports/token_counter.py`
> **关联文档**：[types.md](../../../docs/shared_doc/types.md) · [architecture.md](../../../docs/architecture.md)

---

## Context

- 架构文档共享内核目标：`types.py（通用类型 / 标识）`，此前为 0 字节空文件。
- 现状：全库无任何集中类型/标识定义——`session_id`/`user_id` 是裸 `str` 散落 20+ 处（SessionManager 9 方法、ContextManager、AgentContext、路由、schemas），无 `TypeAlias`/`NewType`。
- 产品导向：Task/TaskState 枚举（Phase C 任务模型地基）当前零消费方，现在建是堆砌——**只做有真实消费方的最小集**。

## Decision

**在 `app/shared/types.py` 定义最小集：`SessionId`/`UserId`（NewType）+ `Messages`（TypeAlias），改造核心签名标注。**

1. **`NewType` 而非强类型类**：运行时恒等返回 str（`SessionId("s1") is "s1"`）、零开销，与存量裸 `str` 完全兼容——签名从 `str` 改 `SessionId` 后测试零断裂（test_session_manager 等传裸 str 不受影响）。
2. **`Messages` 用 `typing.TypeAlias` 而非 PEP 695 `type` 语句**：项目零 PEP 695 用法（23 文件走 typing 侧），保持风格统一；Python 3.14 两者皆可，选保守一致。
3. **改造范围 = 签名标注，边界保持 `str`**：
   - 可改：SessionManager 9 方法（含 delete_session/hard_delete_session 补标注）、`ContextManager.build_messages`、`AgentContext` 字段、端口契约（llm_gateway ×3 + token_counter）
   - 边界保持 `str`：Pydantic schemas、`deps.get_current_user` 返回值、路由 HTTP 参数、SQLAlchemy Column、dict key——不改造（API/持久化边界是 str 的天然接口）
4. **明确不做**：`TaskId`/`TaskState`（Phase C 随任务模型落地）、`BatchId`（工具领域低价值）、`list[dict[str, str]]` 第二别名（消息三种形状，先统一最泛用的 `list[dict]`）。
5. **类型检查器债（已知且接受）**：改造后存量传裸 `str` 在类型检查器视角是「str 传给 SessionId」——项目未启用类型检查器 gate，不阻塞；引入后逐步迁移（NewType 不会在运行时强制，迁移是纯标注替换）。
6. **工业级参照**：Go `type SessionID string`、TypeScript branded types、Python `NewType` 是标识符类型化的标准做法——运行时零成本、类型层面区分用途。

## Consequences

- **正面**：标识符语义进入类型系统（区分 session/user 用途）；消息别名统一端口契约表达；测试零断裂（NewType 恒等）；为 Phase C TaskId/TaskState 提供既有模式。
- **负面**：存量裸 str 与 NewType 标注在类型检查器视角不一致（历史债，未启用 gate 不阻塞）；引入标识类型后新代码需遵守标注（否则检查器报错）。
