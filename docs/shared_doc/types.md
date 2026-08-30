# 通用类型 / 标识说明文档

> **更新日期**：2026-08-24
> **模块**：`app/shared/types.py`
> **文档定位**：共享内核通用类型 —— 标识符（NewType）与消息类型别名，避免跨模块重复定义裸 str。

---

## 📋 目录

- [通用类型 / 标识说明文档](#通用类型--标识说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [定位与职责](#定位与职责)
    - [与其它类型体系的关系](#与其它类型体系的关系)
  - [类型清单](#类型清单)
  - [使用边界](#使用边界)
  - [使用示例](#使用示例)
  - [明确不做（YAGNI）](#明确不做yagni)
  - [相关文档](#相关文档)

---

## 模块概述

### 定位与职责

`app/shared/types.py` 集中定义跨模块复用的**标识符**与**类型别名**：

1. **`SessionId` / `UserId`（NewType）**：标识符类型。运行时是恒等函数（`SessionId("s1")` 返回 `"s1"` 本身，零开销），纯类型标注——让类型检查器区分「会话标识」与「用户标识」等不同用途的 str，避免把 `user_id` 当 `session_id` 传。
2. **`Messages`（PEP 695 `type` 别名）**：LLM 消息列表别名（`type Messages = list[dict]`），在端口契约处统一表达「OpenAI messages 格式」。

### 与其它类型体系的关系

[class-design.md](class-design.md) 讲「六种类类型 + 实例形态」（类怎么用）；types.md 讲「通用类型 / 标识」（跨模块共享的类型值）——两个主题，各自成文。

---

## 类型清单

| 类型 | 定义 | 用途 |
| --- | --- | --- |
| `SessionId` | `NewType("SessionId", str)` | 会话标识（SessionManager 方法参数、AgentContext 字段） |
| `UserId` | `NewType("UserId", str)` | 用户标识（SessionManager 方法参数、AgentContext 字段） |
| `Messages` | `type Messages = list[dict]`（PEP 695） | LLM 消息列表（llm_gateway 端口契约） |

---

## 使用边界

**签名标注可改**（函数/方法入参、dataclass 字段）→ 用 NewType：

- `SessionManager` 全部含 session_id/user_id 的方法（create_session / get_session / get_messages / add_message / delete_session / hard_delete_session / list_sessions / list_sessions_v2 / _get_session_stats）
- `ContextManager.build_messages(session_id)`
- `AgentContext.session_id` / `user_id`

**边界保持 `str`**（不可改）：

- Pydantic 请求/响应 schema 字段（API 边界）
- `deps.get_current_user` 返回值（DI 产出裸 str）
- 路由 HTTP 参数、`X-Session-Id` header
- SQLAlchemy Column
- dict key（如 `session["user_id"]`）

> **NewType 运行时兼容**：`SessionId("s1")` 恒等返回 `"s1"`，存量传裸 str 的调用与测试不受影响（签名标注不影响运行时行为）。

---

## 使用示例

```python
from app.shared.types import SessionId, UserId, Messages

# 签名标注（运行时行为与 str 完全一致）
async def get_session(self, session_id: SessionId) -> dict | None: ...

# 消息列表别名（端口契约）
async def generate(self, messages: Messages, ...) -> StreamResult | None: ...
```

---

## 明确不做（YAGNI）

- `TaskId` / `TaskState` 枚举：Phase C 任务模型的地基，当前零消费方，与任务模型一起落地
- `BatchId`：RCA 工具领域标识，工具内部低价值
- `list[dict[str, str]]` 第二别名：当前消息有三种形状，先统一最泛用的 `list[dict]`

---

## 相关文档

- [class-design.md](class-design.md)（类的类型体系与实例形态）
- [架构设计](../architecture.md)（共享内核 types.py 定位）
- [SessionManager 会话管理](../application_doc/session_doc/session.md)（SessionId/UserId 消费方）
