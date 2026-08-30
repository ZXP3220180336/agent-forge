# 路由模块对外接口文档

> **对应代码**：`app/api/routes/`
> **更新日期**：2026-08-29
> **文档定位**：路由模块对外接口文档——端点契约（请求 / 响应模型 / 认证 / 异常）+ 内部组件导航；服务对象为路由的外部调用方（客户端 / 前端）
> **实现状态**：chat / session ✅ 已实现 · admin / agent / tool ⬜ 预留
> **配套**：错误信封经 [middleware.md](../middleware_doc/middleware.md)（error_handler）；SSE 帧格式见 [events.md](../../shared_doc/events.md)；层总览见 [README.md](../README.md)

---

## 📋 目录

- [路由模块对外接口文档](#路由模块对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心定位](#核心定位)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [对外接口](#对外接口)
    - [端点总览](#端点总览)
    - [认证方式](#认证方式)
    - [会话管理 API](#会话管理-api)
    - [聊天 API](#聊天-api)
    - [对外异常契约](#对外异常契约)
  - [内部实现组织](#内部实现组织)
  - [依赖注入](#依赖注入)
    - [依赖函数一览](#依赖函数一览)
  - [配置关联](#配置关联)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心定位

路由模块是 API 的**对外暴露层**，位于中间件之后、服务层之前，承担「协议适配」职责：

- **暴露 REST API**：将 HTTP 请求 / 响应与内部领域模型互转，定义请求校验模型（Pydantic）与接口语义
- **鉴权后驱动 Agent**：通过 `app/api/deps.py` 注入当前用户与各服务单例，在完成会话授权后驱动 Agent 闭环（LLM 思考 → 工具调用 → LLM 总结）
- **薄路由原则**：路由函数只做「参数校验 → 服务编排 → 响应组装」，业务逻辑下沉到服务层（SessionManager / ContextManager / TaskService 等），不承载领域实现

路由模块与相邻层的职责边界：

| 层 | 职责 | 关键差异 |
| --- | --- | --- |
| 中间件（`app/api/middleware/`） | 请求前置横切处理 | 全局生效，不依赖路由显式声明 |
| 路由模块（`app/api/routes/`） | 端点定义、协议适配、服务编排 | 按端点精确控制，显式声明依赖 |
| 应用层（`app/application/`）与集成层（`app/integration/`） | 领域逻辑、数据访问 | 不感知 HTTP，供路由驱动 |

### 模块结构

```text
app/api/routes/
├── __init__.py   ← 聚合导出：chat_router / session_router，供 main.py 注册
├── chat.py       ← ✅ 已实现：聊天（SSE 流式发送 + 停止）
├── session.py    ← ✅ 已实现：会话 CRUD
├── admin.py      ← 预留空文件：管理接口
├── agent.py      ← 预留空文件：异步任务受理
└── tool.py       ← 预留空文件：工具管理
```

路由注册链路：`app/main.py` 通过 `from app.api.routes import chat_router, session_router` 导入，再 `app.include_router(...)` 挂载。当前仅挂载了两个已实现路由，预留路由完成后需在 `main.py` 追加注册。

### 设计原则

1. **无状态路由**：路由函数不持有跨请求状态，所有依赖（用户、服务）通过 FastAPI 依赖注入按请求获取
2. **Agent 无状态化**：每次请求新建 `ReActAgent` 实例，上下文通过 `AgentContext` 传入，避免 Agent 跨请求状态污染
3. **鉴权前置**：每个受保护端点都注入 `get_current_user`，并在访问会话前校验 `user_id` 归属（403 无权访问）
4. **统一错误语义**：路由不抛 `HTTPException`，抛统一异常树（`AppError` 子类），由 error_handler 翻译为信封（见 [middleware.md](../middleware_doc/middleware.md)）

### 依赖关系

```text
客户端 / 前端
    ↓ HTTP
路由（chat / session）
    ↓ deps 注入（get_current_user + 服务单例）
服务层：SessionManager / ContextManager / TaskService / LLMService / ToolService
    ↓
SSE 事件流回客户端（chat/send）
```

---

## 对外接口

### 端点总览

| 端点 | 方法 | 功能 |
| --- | --- | --- |
| `/api/health` | GET | 健康检查（`app/main.py` 定义）：`{"status": "ok", "version": "1.0.0"}` |
| `/api/session/create` | POST | 创建会话 |
| `/api/session/{session_id}` | GET | 获取会话详情 |
| `/api/session/{session_id}/history` | GET | 获取会话历史（分页） |
| `/api/sessions` | GET | 获取用户会话列表 |
| `/api/session/{session_id}` | DELETE | 删除会话（软删除） |
| `/api/chat/send` | POST | 发送消息（SSE 流式） |
| `/api/chat/stop` | POST | 停止生成（优雅取消运行中 Agent） |

**Base URL**：`http://localhost:8000`（`app/main.py` uvicorn 默认端口）。所有路由 `router = APIRouter(prefix="/api", ...)`。

### 认证方式

当前认证由 `app/api/deps.py` 的 `get_current_user()` **模拟实现**（非 JWT/OAuth）：

```text
Authorization: Bearer <token>
→ 解析出 user_id = "user_" + authorization 头前 8 字符（含 Bearer 前缀，非纯 token）
```

- 缺少 `Authorization` 头 → `401 未授权`
- 实际项目将替换为 JWT 验证（规划见 [middleware.md auth](../middleware_doc/middleware.md)）

### 会话管理 API

所有端点注入 `get_current_user` + `get_session_manager`；除 `POST /session/create` 外的读取 / 删除端点都先做会话存在性（404）与归属（403）校验。

#### `POST /api/session/create` — 创建会话

**请求模型** `CreateSessionRequest`（`app/api/schemas/request.py`）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `system_prompt` | `str \| None` | 否 | `None`（服务层兜底默认值） | 系统提示词 |
| `title` | `str \| None` | 否 | `None`（服务层兜底「新对话」） | 会话标题 |

**响应模型** `CreateSessionResponse`（`app/api/schemas/response.py`）：`{ "session_id": str, "title": str, "created_at": str }`

处理流程：`session_manager.create_session(user_id, system_prompt, title)` → 组装响应模型返回。`title` 兜底为 `"新对话"`。

#### `GET /api/session/{session_id}` — 获取会话详情

返回会话完整信息（`id` / `user_id` / `system_prompt` / `created_at` / `message_count` / `total_tokens` 等，由 SessionManager 提供）。

处理流程：`session_manager.get_session(session_id)`，不存在 → `404 会话不存在`；`session["user_id"] != user_id` → `403 无权访问`。

#### `GET /api/session/{session_id}/history` — 获取会话历史

**查询参数**：

| 参数 | 类型 | 默认 | 约束 | 说明 |
| --- | --- | --- | --- | --- |
| `limit` | `int` | `50` | `le=200` | 返回消息条数上限 |
| `offset` | `int` | `0` | `ge=0` | 分页偏移 |

响应：`{ "session_id": "...", "messages": [{"role": "user|assistant", "content": "..."}, ...] }`（仅返回 user / assistant 角色，`created_at` 升序）。

处理流程：会话验证与授权（404 / 403）→ `session_manager.get_messages(session_id, limit, offset)`。

#### `GET /api/sessions` — 获取用户会话列表

`session_manager.list_sessions(user_id=user_id)` 查询当前用户全部**活跃**会话（软删除过滤），响应：`{ "sessions": [...] }`。

#### `DELETE /api/session/{session_id}` — 删除会话

处理流程：会话验证与授权（404 / 403）→ `session_manager.delete_session(session_id)`（软删除）→ 响应 `{ "message": "会话已删除" }`。

### 聊天 API

#### `POST /api/chat/send` — 发送消息（流式 SSE）

**请求模型** `SendMessageRequest`（`app/api/schemas/request.py`）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `session_id` | `str` | 是 | — | 目标会话 ID |
| `message` | `str` | 是 | — | 用户消息内容 |
| `max_iterations` | `int` | 否 | `10` | Agent 最大迭代次数（ReAct 闭环轮数上限；未传时用 `agent_params` 默认） |
| `stream` | `bool` | 否 | `true` | 是否流式返回（**当前未实际使用**，端点始终以 SSE 流式返回） |

**响应**：`text/event-stream`，逐事件推送（SSE 帧格式见 [events.md](../../shared_doc/events.md)），末尾追加 `data: [DONE]\n\n` 帧。响应头包含 `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Session-Id`。

**处理流程**：

1. **会话验证与授权**：`session_manager.get_session(session_id)`，不存在 → `404 会话不存在`；`session["user_id"] != user_id` → `403 无权访问`
2. **保存用户消息**：`session_manager.add_message(role="user", content=message, token_count=context_manager.count_tokens(message))`，token 数由 ContextManager 经 TokenCounter 端口统计（见 [token_counter.md](../../integration_doc/llm_doc/token_counter.md)）
3. **构建上下文**：`context_manager.build_messages(session_id, user_message)` 组装发送给 LLM 的消息序列
4. **定义流式生成器 `generate()`**：
   - 新建 `AgentContext`（8 字段：`session_id` / `user_id` / `max_iterations` / `temperature` / `max_tokens` / `max_execution_time` / `max_context_rounds` / `max_context_tokens`，运行参数来自 `get_agent_params` 注入，`max_iterations` 可被请求体覆盖）与 `ReActAgent(llm=llm_service, tools=tool_service, context_budget=context_manager)` —— **Agent 无状态**，每次请求新建实例
   - `async for event in task_service.run_agent(user_input, messages, context, agent)` 驱动 ReAct 闭环（LLM 思考 → 工具调用 → LLM 总结），并**在任务级并发信号量 `agent_max_concurrent_tasks` 保护下运行**
   - 每个事件 `yield` 给 `StreamingResponse` 逐帧推送
   - 异常兜底：捕获异常后 `yield build_error_event(...)`，错误以 SSE 事件透出而非中断连接
   - `finally`：先 `yield "data: [DONE]\n\n"` 收尾，再从 `agent.result` 取最终答复，非空时 `session_manager.add_message(role="assistant", content=..., reasoning_content=..., token_count=...)` 持久化
5. **返回 `StreamingResponse`**：`media_type="text/event-stream"`

**依赖注入**（5 个服务 + 1 个参数 + 用户）：

| 依赖 | 用途 |
| --- | --- |
| `get_current_user` | 解析请求身份（user_id），鉴权前置 |
| `get_session_manager` | 会话读取 / 消息持久化 |
| `get_context_manager` | 构建 messages + token 计数 |
| `get_llm_service` | 作为 `ReActAgent` 的 LLM 后端 |
| `get_tool_service` | 提供工具定义与执行（`ReActAgent` 工具侧） |
| `get_task_service` | 在任务级并发约束下运行 Agent |
| `get_agent_params` | 提供 Agent 运行参数（max_iterations / temperature / max_tokens / max_execution_time / max_context_rounds / max_context_tokens） |

#### `POST /api/chat/stop` — 停止生成

`session_id` 为**查询参数**（函数参数未绑定 Pydantic 模型，FastAPI 默认按 query 解析）。

处理流程：会话验证与授权（404 / 403，同 `chat/send`）→ `task_service.cancel_session(session_id)` 置位会话取消事件 → 返回 `{"message": "已发送停止信号", "cancelled": bool}`。

> ✅ **真实优雅取消**：`/chat/stop` 置位 `TaskService` 的会话取消事件（`cancel_event`），运行中的 Agent 在轮次边界感知取消 → `CANCELLED` 分发（优雅停止，不硬中断、保留部分进度）；send 请求结束清理注册表。`cancelled=false` 表示该会话当前无运行任务。链路与语义见 [REASON-003](../../../issues/domain/reasoning/2026-08-30-cancel-event-semantics.md)。

### 对外异常契约

路由层抛出的业务状态码（`AppError` 子类，经 error_handler 翻译为统一信封）：

| 状态码 | 异常 | 触发 |
| --- | --- | --- |
| 401 | `UnauthorizedError` | 缺少 `Authorization` 头 |
| 403 | `ForbiddenError` | 已认证但无权访问该会话（`user_id` 不匹配） |
| 404 | `NotFoundError` | 会话不存在 |

> 统一异常树（`AppError` / `AppErrorCode`）见 [error_handling.md](../../shared_doc/error_handling.md)；信封格式与完整状态映射见 [middleware.md「统一错误信封」](../middleware_doc/middleware.md)。

---

## 内部实现组织

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| 聊天路由 | chat.py | SSE 流式发送（ReAct 闭环）+ 停止（优雅取消） | ✅ |
| 会话路由 | session.py | 会话创建 / 详情 / 历史 / 列表 / 删除 | ✅ |
| 管理路由 | admin.py | 管理接口（系统状态、统计、运维；鉴权需高于普通用户） | ⬜ 预留 |
| 任务路由 | agent.py | 异步任务受理（规划 `POST /api/tasks/submit` + `GET /api/tasks/{id}`，承接 TaskService 调度，演进见 [architecture Phase C](../../architecture.md)） | ⬜ 预留 |
| 工具路由 | tool.py | 工具管理（可基于 ToolService 能力实现） | ⬜ 预留 |

---

## 依赖注入

路由模块通过 `app/api/deps.py` 提供的依赖函数获取服务与用户身份（见 [deps.py](../../../app/api/deps.py)），而非在路由内直接实例化，原因：

1. **单例复用**：`SessionManager` / `ContextManager` / `LLMService` / `ToolService` / `TaskService` 均持有重量级资源（Redis 连接池、数据库连接池、OpenAI 异步客户端），依赖函数返回 `container` 中的全局单例，避免每个请求重复创建
2. **启动期校验**：各 `get_*_service` 在 `container` 对应实例为 `None` 时抛 `RuntimeError`，提示「请确保在应用启动时调用了 `container.initialize()`」——即服务必须在启动时完成初始化
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
| `get_agent_params` | `dict`（Agent 运行参数） | `POST /api/chat/send` |

---

## 配置关联

- Agent 运行参数（`agent_max_iterations` / `llm_temperature` / `llm_max_tokens` / `agent_timeout` / `agent_max_context_rounds` / `max_context_tokens`）经 `container.agent_params` → `get_agent_params` 注入 chat 路由
- 并发约束（`agent_max_concurrent_tasks` / `agent_max_concurrent_tools`）作用于 TaskService / ToolService
- 完整配置项见 [config 文档](../../config_doc/config.md)

---

## 相关文档

- [层总览 README](../README.md)（api 层结构树 / 实现状态）
- [中间件模块](../middleware_doc/middleware.md)（统一错误信封 / auth / rate_limit）
- [SSE 事件格式](../../shared_doc/events.md)（事件类型与帧格式）
- [异常体系](../../shared_doc/error_handling.md)（统一异常树）
- [架构设计](../../architecture.md)（演进路径：Phase C 异步任务 / Phase D 中间件）
- 服务层：Session / Context / Task 模块（`app/application/`，见 [应用层说明](../../application_doc/README.md)）
