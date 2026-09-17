# 路由模块对外接口文档

> **对应代码**：`app/api/routes/`
> **更新日期**：2026-09-16
> **文档定位**：路由模块对外接口文档——端点契约（请求 / 响应模型 / 认证 / 异常）+ 内部组件导航；服务对象为路由的外部调用方（客户端 / 前端）
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
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
- **聊天传输适配**：注入 `ChatService`，把 HTTP 断连转换为取消信号，并输出 SSE error / `[DONE]` 与响应头
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
2. **用例下沉**：聊天预检、运行身份、Agent 创建和结果提交由 Application `ChatService` 负责
3. **鉴权前置**：每个受保护端点注入 `get_current_user`；会话归属由对应应用用例或会话路由校验
4. **统一错误语义**：路由不抛 `HTTPException`，抛统一异常树（`AppError` 子类），由 error_handler 翻译为信封（见 [middleware.md](../middleware_doc/middleware.md)）

### 依赖关系

```text
客户端 / 前端
    ↓ HTTP
路由（chat / session）
    ↓ deps 注入（get_current_user + ChatService / 会话服务）
应用层：ChatService → SessionManager / ContextManager / TaskService → Agent
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

处理流程：会话验证与授权（404 / 403）→ `session_manager.get_messages(session_id, limit, offset)`。历史分页从最新消息窗口向更早消息推进；每页内部仍按消息创建时间正序返回。

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
| `max_iterations` | `int \| null` | 否 | `null` | Agent 最大迭代次数（1..100）；未传时使用装配根的 `agent_max_iterations` |
| `stream` | `bool` | 否 | `true` | 是否流式返回（**当前未实际使用**，端点始终以 SSE 流式返回） |

**响应**：`text/event-stream`，逐事件推送（SSE 帧格式见 [events.md](../../shared_doc/events.md)），末尾追加 `data: [DONE]\n\n` 帧。响应头包含 `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Session-Id`。

**处理流程**：

1. `ChatService.prepare_message(...)` 在响应头前完成会话 404/403、user 提交、上下文快照、run 登记及每请求 Agent 创建。
2. 路由消费 `ChatRun.events()`；逐事件检查 `Request.is_disconnected()`，首次断连转换为会话级取消，之后继续排水但不再推送。
3. 普通运行异常翻译为 SSE error；未断连且未提前关闭时追加一次 `[DONE]`。
4. 路由专用 `_ChatStreamingResponse` 在 ASGI `send()` 失败时也从调用边界关闭 body iterator 与 ChatRun，避免悬挂子流、登记或信号量。
5. `ChatRun.aclose()` 按 run 清登记，并在 `[DONE]` 后保存非空 assistant 结果。

**依赖注入**：

| 依赖 | 用途 |
| --- | --- |
| `get_current_user` | 解析请求身份（user_id），鉴权前置 |
| `get_chat_service` | 获取已装配的聊天用例；内部依赖由 Container 注入 |

> ✅ **客户端被动断连自动取消**：逐事件探测断连时取消该会话当时全部运行并继续排水；若断开发生在 ASGI 实际发送 chunk 的窗口，专用响应边界仍关闭当前 body iterator 和 run。每个 ChatRun 最终只释放自己的登记。链路见 [ChatService 说明](../../application_doc/chat_doc/chat.md)。

#### `POST /api/chat/stop` — 停止生成

`session_id` 为**查询参数**（函数参数未绑定 Pydantic 模型，FastAPI 默认按 query 解析）。

处理流程：`ChatService.stop` 完成会话验证与授权（404 / 403）→ 按 session 置位活动运行 → 返回 `{"message": "已发送停止信号", "cancelled": bool}`。

> ✅ **会话级停止**：`/chat/stop` 保留对外 session 语义，内部置位该会话全部活动 run 的取消事件；一个 run 自然结束只清理自己的登记。工具调用还携带所属 run 的 `run_stop`，该信号只关闭该 run 的新工具准入。`cancelled=false` 表示该会话当前无活动运行。链路与语义见 [REASON-003](../../../issues/domain/reasoning/2026-08-30-cancel-event-semantics.md)。

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
| 聊天路由 | chat.py | HTTP/SSE、断连适配、专用响应关闭边界与停止端点 | [见对齐表](../../ALIGNMENT.md) |
| 会话路由 | session.py | 会话创建 / 详情 / 历史 / 列表 / 删除 | [见对齐表](../../ALIGNMENT.md) |
| 管理路由 | admin.py | 管理接口（系统状态、统计、运维；鉴权需高于普通用户） | [见对齐表](../../ALIGNMENT.md) |
| 任务路由 | agent.py | 异步任务受理（规划 `POST /api/tasks/submit` + `GET /api/tasks/{id}`，承接 TaskService 调度，演进见 [architecture Phase C](../../project/architecture.md)） | [见对齐表](../../ALIGNMENT.md) |
| 工具路由 | tool.py | 工具管理（可基于 ToolService 能力实现） | [见对齐表](../../ALIGNMENT.md) |

---

## 依赖注入

路由模块通过 `app/api/deps.py` 提供的依赖函数获取服务与用户身份（见 [deps.py](../../../app/api/deps.py)），而非在路由内直接实例化，原因：

1. **单例复用**：聊天路由只注入 `ChatService`；其 Session、Context、Task、LLM、Tool 与配置依赖在 Container 统一装配
2. **启动期校验**：各 `get_*_service` 在 `container` 对应实例为 `None` 时抛 `RuntimeError`，提示「请确保在应用启动时调用了 `container.initialize()`」——即服务必须在启动时完成初始化
3. **测试友好**：路由函数显式声明依赖，便于在测试中替换实现

### 依赖函数一览

| 依赖 | 返回 | 注入端点 |
| --- | --- | --- |
| `get_current_user` | `str`（user_id） | 所有端点 |
| `get_chat_service` | `ChatService` | `POST /api/chat/send`、`POST /api/chat/stop` |
| `get_session_manager` | `SessionManager` | session 路由 |

---

## 配置关联

- Agent 运行参数（含 `agent_max_iterations`、`agent_max_tool_protocol_retries` 与其它模型/执行/上下文护栏）由 `container.agent_params` 注入 `ChatService`
- 并发约束（`agent_max_concurrent_tasks` / `agent_max_concurrent_tools`）作用于 TaskService / ToolService
- 完整配置项见 [config 文档](../../config_doc/config.md)

---

## 相关文档

- [层总览 README](../README.md)（api 层结构树 / 实现状态）
- [中间件模块](../middleware_doc/middleware.md)（统一错误信封 / auth / rate_limit）
- [SSE 事件格式](../../shared_doc/events.md)（事件类型与帧格式）
- [异常体系](../../shared_doc/error_handling.md)（统一异常树）
- [架构设计](../../project/architecture.md)（演进路径：Phase C 异步任务 / Phase D 中间件）
- 服务层：Session / Context / Task 模块（`app/application/`，见 [应用层说明](../../application_doc/README.md)）
