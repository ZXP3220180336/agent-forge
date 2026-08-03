# 路由层说明文档

> **更新日期**：2026-08-04
> **文档定位**：路由层（`app/api/routes/`）的定位、已实现路由的端点/请求模型/处理流程、预留路由规划与依赖注入方式。
> **当前已实现**：`chat.py`（聊天，SSE 流式）、`session.py`（会话 CRUD）。`admin.py` / `agent.py` / `tool.py` 均为**预留空文件**，尚未落地任何逻辑。

---

## 📋 目录

- [模块概述](#模块概述)
- [模块结构](#模块结构)
- [实现状态表](#实现状态表)
- [已实现路由详解](#已实现路由详解)
  - [chat — 聊天路由](#chat--聊天路由)
  - [session — 会话管理路由](#session--会话管理路由)
- [预留路由说明](#预留路由说明)
  - [admin — 管理接口](#admin--管理接口)
  - [agent — 异步任务受理](#agent--异步任务受理)
  - [tool — 工具管理](#tool--工具管理)
- [依赖注入](#依赖注入)
- [相关文档](#相关文档)
- [当前进度与遗留](#当前进度与遗留)

---

## 模块概述

### 核心定位

路由层是 API 的**对外暴露层**，位于中间件之后、服务层之前，承担「协议适配」职责：

- **暴露 REST API**：将 HTTP 请求 / 响应与内部领域模型互转，定义请求校验模型（Pydantic）与接口语义
- **鉴权后驱动 Agent**：通过 `app/dependencies.py` 注入当前用户与各服务单例，在完成会话授权后驱动 Agent 闭环（LLM 思考 → 工具调用 → LLM 总结）
- **薄路由原则**：路由函数只做「参数校验 → 服务编排 → 响应组装」，业务逻辑下沉到服务层（SessionManager / ContextManager / TaskService 等），不承载领域实现

路由层与相邻层的职责边界：

| 层 | 职责 | 关键差异 |
| --- | --- | --- |
| 中间件（`app/api/middleware/`） | 请求前置横切处理 | 全局生效，不依赖路由显式声明 |
| 路由层（`app/api/routes/`） | 端点定义、协议适配、服务编排 | 按端点精确控制，显式声明依赖 |
| 服务层（`app/services/`） | 领域逻辑、数据访问 | 不感知 HTTP，供路由驱动 |

### 模块结构

```
app/api/routes/
├── __init__.py   ← 聚合导出：chat_router / session_router，供 main.py 注册
├── chat.py       ← ✅ 已实现：聊天（SSE 流式发送 + 停止）
├── session.py    ← ✅ 已实现：会话 CRUD
├── admin.py      ← 预留空文件：管理接口
├── agent.py      ← 预留空文件：异步任务受理
└── tool.py       ← 预留空文件：工具管理
```

路由注册链路：`app/main.py` 通过 `from app.api.routes import chat_router, session_router` 导入，再 `app.include_router(...)` 挂载（见 [api.md](../api.md)）。当前仅挂载了两个已实现路由，预留路由完成后需在 `main.py` 追加注册。

### 设计原则

1. **无状态路由**：路由函数不持有跨请求状态，所有依赖（用户、服务）通过 FastAPI 依赖注入按请求获取
2. **Agent 无状态化**：每次请求新建 `ReActAgent` 实例，上下文通过 `AgentContext` 传入，避免 Agent 跨请求状态污染
3. **鉴权前置**：每个受保护端点都注入 `get_current_user`，并在访问会话前校验 `user_id` 归属（403 无权访问）
4. **统一错误语义**：遵循 api_doc「错误处理」约定——`401` 未授权、`403` 无权访问、`404` 会话不存在

---

## 实现状态表

| 文件 | 状态 | 端点 | 规划功能 |
| ---- | ---- | ---- | -------- |
| `app/api/routes/chat.py` | ✅ 已实现 | `POST /api/chat/send`、`POST /api/chat/stop` | 聊天（SSE 流式）、停止生成 |
| `app/api/routes/session.py` | ✅ 已实现 | `POST /api/session/create`、`GET /api/session/{id}`、`GET /api/session/{id}/history`、`GET /api/sessions`、`DELETE /api/session/{id}` | 会话 CRUD |
| `app/api/routes/admin.py` | 预留空文件 | — | 管理接口（系统状态、统计、运维） |
| `app/api/routes/agent.py` | 预留空文件 | — | 异步任务受理（承接 TaskService 调度） |
| `app/api/routes/tool.py` | 预留空文件 | — | 工具管理 |

---

## 已实现路由详解

### chat — 聊天路由

**文件**：`app/api/routes/chat.py`（约 143 行）
**路由定义**：`router = APIRouter(prefix="/api", tags=["聊天"])`

#### `POST /api/chat/send` — 发送消息（流式 SSE）

**请求模型** `SendMessageRequest`：

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | `str` | 是 | — | 目标会话 ID |
| `message` | `str` | 是 | — | 用户消息内容 |
| `max_iterations` | `int` | 否 | `10` | Agent 最大迭代次数（ReAct 闭环轮数上限） |
| `stream` | `bool` | 否 | `true` | 是否流式返回（**当前未实际使用**，端点始终以 SSE 流式返回，见「遗留未定事项」） |

**响应**：`text/event-stream`，逐事件推送（事件格式见 [api.md「SSE 事件格式」](../api.md)），末尾追加 `data: [DONE]\n\n` 帧。响应头包含 `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Session-Id`。

**处理流程**：

1. **会话验证与授权**：`session_manager.get_session(session_id)` 获取会话，不存在 → `404 会话不存在`；`session["user_id"] != user_id` → `403 无权访问`
2. **保存用户消息**：`session_manager.add_message(role="user", content=message, token_count=context_manager.count_tokens(message))`，token 数由 ContextManager 的 tiktoken 编码器统计
3. **构建上下文**：`context_manager.build_messages(session_id, user_message)` 组装发送给 LLM 的消息序列
4. **定义流式生成器 `generate()`**：
   - 新建 `AgentContext(session_id, user_id, max_iterations)` 与 `ReActAgent(llm=llm_service, tools=tool_service)` —— **Agent 无状态**，每次请求新建实例
   - `async for event in task_service.run_agent(user_input, messages, context, agent)` 驱动 ReAct 闭环（LLM 思考 → 工具调用 → LLM 总结），并**在任务级并发信号量 `agent_max_concurrent_tasks` 保护下运行**
   - 每个事件 `yield` 给 `StreamingResponse` 逐帧推送
   - 异常兜底：捕获异常后 `yield build_error_event(...)`，错误以 SSE 事件透出而非中断连接
   - `finally`：先 `yield "data: [DONE]\n\n"` 收尾，再从 `agent.result` 取最终答复，非空时 `session_manager.add_message(role="assistant", content=..., reasoning_content=..., token_count=...)` 持久化
5. **返回 `StreamingResponse`**：`media_type="text/event-stream"`

**依赖注入**（5 个服务 + 用户）：

| 依赖 | 用途 |
| --- | --- |
| `get_current_user` | 解析请求身份（user_id），鉴权前置 |
| `get_session_manager` | 会话读取 / 消息持久化 |
| `get_context_manager` | 构建 messages + token 计数 |
| `get_llm_service` | 作为 `ReActAgent` 的 LLM 后端 |
| `get_tool_service` | 提供工具定义与执行（`ReActAgent` 工具侧） |
| `get_task_service` | 在任务级并发约束下运行 Agent |

#### `POST /api/chat/stop` — 停止生成

`session_id` 为**查询参数**（函数参数未绑定 Pydantic 模型，FastAPI 默认按 query 解析）。

流程：会话验证与授权（404 / 403，同 `chat/send`）→ 返回 `{"message": "已发送停止信号"}`。

> ⚠️ **当前为占位实现**：仅校验会话归属并返回固定响应，**未实际取消**正在进行的 Agent 生成。代码注释明确标注「实际项目中，这里会调用 LLMService 的 cancel 方法」，即真正的取消能力（如 `asyncio.Task` 取消或生成器 `aclose`）尚未落地。

---

### session — 会话管理路由

**文件**：`app/api/routes/session.py`（约 117 行）
**路由定义**：`router = APIRouter(prefix="/api", tags=["会话管理"])`

所有端点均注入 `get_current_user` + `get_session_manager`；除 `POST /session/create` 外的读取 / 删除端点都先做会话存在性（404）与归属（403）校验。

#### `POST /api/session/create` — 创建会话

**请求模型** `CreateSessionRequest`：

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `system_prompt` | `str \| None` | 否 | `None`（服务层兜底默认值） | 系统提示词 |
| `title` | `str \| None` | 否 | `None`（服务层兜底「新对话」） | 会话标题 |

**响应模型** `CreateSessionResponse`：`{ "session_id": str, "title": str, "created_at": str }`

流程：`session_manager.create_session(user_id, system_prompt, title)` → 组装响应模型返回。`title` 兜底为 `"新对话"`。

#### `GET /api/session/{session_id}` — 获取会话详情

返回会话完整信息（`id` / `user_id` / `system_prompt` / `created_at` / `message_count` / `total_tokens` 等，由 SessionManager 提供）。

#### `GET /api/session/{session_id}/history` — 获取会话历史

**查询参数**：

| 参数 | 类型 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | `int` | `50` | `le=200` | 返回消息条数上限 |
| `offset` | `int` | `0` | `ge=0` | 分页偏移 |

响应：`{ "session_id": "...", "messages": [{"role": "user|assistant", "content": "..."}, ...] }`

#### `GET /api/sessions` — 获取用户会话列表

`session_manager.list_sessions(user_id=user_id)` 查询当前用户全部会话，响应：`{ "sessions": [...] }`。

> 代码注释提示：实际实现从数据库查询该用户所有**活跃**会话（软删除过滤）。

#### `DELETE /api/session/{session_id}` — 删除会话

`session_manager.delete_session(session_id)`（软删除），响应：`{ "message": "会话已删除" }`。

---

## 预留路由说明

以下路由文件已创建但未实现（0 字节），用于锁定模块边界，预期功能如下（与 [api.md「预留路由」](../api.md) 一致）：

### admin — 管理接口

**文件**：`app/api/routes/admin.py`（预留）

规划：系统管理类接口，如服务状态、运行统计、运维操作。属于后台管理范畴，落地时需注意鉴权级别高于普通用户接口（建议在依赖注入层面增加管理员角色校验，或依赖中间件层的 JWT 鉴权扩展）。

### agent — 异步任务受理

**文件**：`app/api/routes/agent.py`（预留）

规划：异步任务受理接口，如 `POST /api/tasks/submit`（提交任务）+ `GET /api/tasks/{id}`（查询任务状态/结果），承接 `TaskService` 的调度能力，将当前 `chat/send` 的「同步 SSE」形态扩展为「提交即返回、异步查结果」的形态。设计时需与 [task 模块](../../service_doc/task_doc/task.md) 的并发信号量约束对齐。

### tool — 工具管理

**文件**：`app/api/routes/tool.py`（预留）

规划：工具管理接口（如工具列表 / 启用禁用），可基于 `ToolService` 的能力实现。落地时参考 [tool 模块](../../tool_doc/tools.md)。

---

## 依赖注入

路由层通过 `app/dependencies.py` 提供的依赖函数获取服务与用户身份（见 [dependencies.py](../../../app/dependencies.py)），而非在路由内直接实例化，原因：

1. **单例复用**：`SessionManager` / `ContextManager` / `LLMService` / `ToolService` / `TaskService` 均持有重量级资源（Redis 连接池、数据库连接池、OpenAI 异步客户端），依赖函数返回 `app_state` 中的全局单例，避免每个请求重复创建
2. **启动期校验**：各 `get_*_service` 在 `app_state` 对应实例为 `None` 时抛 `RuntimeError`，提示「请确保在应用启动时调用了 `app_state.initialize()`」——即服务必须在启动时完成初始化
3. **测试友好**：路由函数显式声明依赖，便于在测试中替换实现

### 依赖函数一览

| 依赖 | 返回 | 注入端点 |
| --- | --- | --- |
| `get_current_user` | `str`（user_id） | 所有端点 |
| `get_session_manager` | `SessionManager` | chat / session 所有端点 |
| `get_context_manager` | `ContextManager` | `POST /api/chat/send` |
| `get_llm_service` | `LLMService` | `POST /api/chat/send` |
| `get_tool_service` | `ToolService` | `POST /api/chat/send` |
| `get_task_service` | `TaskService` | `POST /api/chat/send` |

### 认证实现（当前形态）

`get_current_user` 当前为**模拟 Token 解析**（非 JWT）：

```python
async def get_current_user(authorization: str = Header(None)) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="未授权")
    return "user_" + authorization[:8]
```

- 缺少 `Authorization: Bearer <token>` 头 → `401 未授权`
- 有 Token 时不做签名 / 有效期校验，仅字符串截取拼出 `user_id`（`"user_" + token 前 8 字符`）
- 代码注释明确「实际项目使用 JWT/OAuth」，迁移到中间件层是既定规划（见 [middleware_doc/auth 设计要点](../middleware_doc/middleware.md)）

---

## 相关文档

| 文档 | 链接 | 关联内容 |
| ---- | ---- | -------- |
| API 说明文档 | [api.md](../api.md) | 总览：端点清单、SSE 事件格式、认证方式、错误处理约定 |
| 中间件层说明文档 | [middleware.md](../middleware_doc/middleware.md) | 认证从依赖注入迁移到中间件的规划、限流 / 统一异常 |
| Agent 层说明文档 | [agent.md](../../core_doc/agent_doc/agent.md) | `ReActAgent` 闭环：LLM 思考 → 工具调用 → LLM 总结 |
| AgentContext / Agent 上下文 | [core.md](../../core_doc/core.md) | Agent 上下文与迭代上限（`max_iterations`）语义 |
| LLM 层说明文档 | [llm.md](../../service_doc/llm_doc/llm.md) | LLMService 封装、流式事件产出（reasoning / message） |
| Task 服务说明文档 | [task.md](../../service_doc/task_doc/task.md) | TaskService 任务级并发信号量（`agent_max_concurrent_tasks`） |
| Tool 模块说明文档 | [tools.md](../../tool_doc/tools.md) | ToolService 工具定义与执行、预留 `tool.py` 参考 |
| 项目架构 | [architecture.md](../../architecture.md) | 系统架构与模块边界 |
| 项目总览 | [HANDOFF.md](../../HANDOFF.md) | 项目整体进度与遗留事项 |

---

## 当前进度与遗留

> 本节记录路由层的进度与下一步计划（项目整体进度见 [HANDOFF.md](../../HANDOFF.md)）。

### 已实现

- `chat.py`：`POST /api/chat/send`（SSE 流式，含 ReAct 闭环、异常兜底、回复持久化）+ `POST /api/chat/stop`（会话校验占位）
- `session.py`：会话创建 / 详情 / 历史（分页）/ 列表 / 删除（软删除）五个端点
- 路由注册链路打通：`__init__.py` 聚合导出 → `main.py` `include_router`
- 依赖注入通路就绪：chat 路由已串联 SessionManager / ContextManager / LLMService / ToolService / TaskService 五服务

### 遗留未定事项

| 事项 | 当前状态 | 说明 |
| ---- | -------- | ---- |
| `chat/stop` 实际取消 | 未实现 | 当前仅返回占位响应，需接入生成任务取消（如 `asyncio.Task.cancel` / 生成器 `aclose` / LLMService 的 cancel） |
| `SendMessageRequest.stream` 字段 | 未使用 | 端点恒以 SSE 流式返回，`stream=False` 时是否走非流式（一次性 JSON 响应）待决策 |
| `agent.py` 异步任务受理 | 未开始 | 决定是否将「同步 SSE」扩展为「提交 + 异步查询」形态，与 TaskService 调度对齐 |
| `admin.py` 管理接口 | 未开始 | 需明确管理员鉴权模型 |
| `tool.py` 工具管理 | 未开始 | 基于 ToolService 的能力清单待梳理 |
| 认证形态 | 依赖注入模拟 | 迁移到中间件 JWT 认证的路径见 [middleware_doc](../middleware_doc/middleware.md) |

### 下一步计划

1. 实现 `chat/stop` 的真实取消能力，串起生成任务的取消链路
2. 明确 `stream` 字段语义（或移除该字段避免误导）
3. 实现 `agent.py` 异步任务受理，扩展 TaskService 的对外形态
4. 按需实现 `admin.py` / `tool.py`，并在 `main.py` 注册
