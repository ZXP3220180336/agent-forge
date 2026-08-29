# API 层说明文档

> **对应代码**：`app/api/`
> **更新日期**：2026-08-29
> **文档定位**：API 层（`app/api/`）—— 系统对外暴露边界，HTTP 协议适配 + 鉴权 + 统一错误信封；是客户端（前端 / 外部系统）与服务层的桥梁。
> **实现状态**：路由（✅ chat / session）· 中间件（🔶 error_handler ✅ + auth / rate_limit 预留）· Schema（🔶 request / response）
> **配套**：端点契约见 [routes.md](routes_doc/routes.md) · 错误信封见 [middleware.md](middleware_doc/middleware.md)

---

## 📋 目录

- [API 层说明文档](#api-层说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [实现状态总览](#实现状态总览)
  - [路由层（routes）](#路由层routes)
  - [中间件层（middleware）](#中间件层middleware)
  - [Schema 与依赖注入](#schema-与依赖注入)
  - [典型调用链路](#典型调用链路)
  - [配置关联](#配置关联)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

API 层是系统的**对外暴露边界**，位于客户端与服务层之间，负责：

- **HTTP 协议适配**：将 HTTP 请求 / 响应与内部领域模型互转，定义请求校验模型（Pydantic）与接口语义
- **路由编排**：`routes/` 定义端点，经依赖注入驱动服务层（SessionManager / ContextManager / TaskService / LLMService / ToolService）
- **横切处理**：`middleware/` 统一处理认证、限流与异常（当前 error_handler 已落地，认证 / 限流预留）
- **数据契约**：`schemas/` 集中管理请求 / 响应 DTO，路由层只 import 使用
- **统一错误信封**：`AppError` 经 error_handler 翻译为 `{code, message, details}`（业务码与 HTTP 状态解耦）

### 模块结构

```text
app/api/
├── deps.py                 ← 依赖注入函数（get_current_user / get_*_service / get_agent_params）
├── routes/                 ← 路由层：端点定义 + 协议适配 + 服务编排
│   ├── __init__.py         ← 聚合导出 chat_router / session_router
│   ├── chat.py             ← ✅ 聊天（SSE 流式发送 + 停止）
│   ├── session.py          ← ✅ 会话 CRUD
│   ├── admin.py            ← ⬜ 预留：管理接口
│   ├── agent.py            ← ⬜ 预留：异步任务受理
│   └── tool.py             ← ⬜ 预留：工具管理
├── middleware/             ← 中间件层：请求前置横切处理
│   ├── error_handler.py    ← ✅ 统一异常处理（AppError → HTTP 状态 + 信封）
│   ├── auth.py             ← ⬜ 预留：JWT 认证
│   └── rate_limit.py       ← ⬜ 预留：API 限流
└── schemas/                ← Schema 层：请求/响应 DTO
    ├── request.py          ← ✅ 请求 DTO（SendMessageRequest / CreateSessionRequest）
    ├── response.py         ← ✅ 响应 DTO（CreateSessionResponse）
    └── agent.py            ← ⬜ 预留：Agent DTO
```

入口装配在 `app/main.py`：`include_router(chat_router)` + `include_router(session_router)` + `register_error_handlers(app)`。

### 设计原则

1. **薄路由**：路由函数只做「参数校验 → 服务编排 → 响应组装」，业务逻辑下沉服务层，不承载领域实现
2. **依赖倒置注入**：服务经 `deps.py` 从 `container` 全局单例按请求注入，路由内不直接实例化
3. **Agent 无状态化**：每次请求新建 `ReActAgent` 实例，上下文经 `AgentContext` 传入
4. **鉴权前置**：受保护端点注入 `get_current_user`，访问会话前校验 `user_id` 归属（403）
5. **统一错误语义**：API 层不抛 `HTTPException`，全走统一异常树（`AppError`），error_handler 在对外边界翻译

### 依赖关系

```text
客户端 / 前端
    ↓ HTTP
app/main.py（装配路由 + 注册 error_handler + CORS / SPA 回退）
    ↓
app/api/（deps 注入用户身份 + 服务单例）
    ↓
app/application/（SessionManager / ContextManager / TaskService）
app/integration/（LLMService / ToolService / EmbeddingService）
```

API 层不感知领域实现，只依赖服务层接口与共享内核（`app/shared/` 的异常 / 事件 / 类型）。

---

## 实现状态总览

| 子模块 | 文件 | 状态 | 核心内容 |
| --- | --- | --- | --- |
| 依赖注入 | deps.py | 🔶 | DI 函数：用户身份 + 服务单例 + agent 参数（经 chat_flow 间接覆盖） |
| 路由 | chat.py / session.py | ✅ | 聊天 SSE / 会话 CRUD（见 [routes.md](routes_doc/routes.md)） |
| 路由 | admin.py / agent.py / tool.py | ⬜ | 预留空文件 |
| 中间件 | error_handler.py | ✅ | AppError → HTTP 状态 + `{code, message, details}` 信封（见 [middleware.md](middleware_doc/middleware.md)） |
| 中间件 | auth.py / rate_limit.py | ⬜ | 预留空文件（认证当前由 deps 模拟） |
| Schema | request.py / response.py | 🔶 | 请求 / 响应 DTO（随路由测试覆盖） |
| Schema | agent.py | ⬜ | 预留空文件（Agent DTO） |

---

## 路由层（routes）

**代码**：`app/api/routes/` · **文档**：[路由模块对外接口文档](routes_doc/routes.md)

API 的**对外暴露层**，承担协议适配与服务编排：

| 组件 | 文件 | 端点 | 状态 |
| --- | --- | --- | --- |
| 聊天 | chat.py | `POST /api/chat/send`（SSE）、`POST /api/chat/stop` | ✅ |
| 会话 | session.py | `POST /api/session/create`、`GET /api/session/{id}`、`GET /api/session/{id}/history`、`GET /api/sessions`、`DELETE /api/session/{id}` | ✅ |
| 管理 / 任务 / 工具 | admin.py / agent.py / tool.py | —（规划） | ⬜ |

端点契约（请求 / 响应模型、认证方式、SSE 帧格式）见 [routes.md](routes_doc/routes.md) 与 [events.md](../shared_doc/events.md)。

---

## 中间件层（middleware）

**代码**：`app/api/middleware/` · **文档**：[中间件模块对外接口文档](middleware_doc/middleware.md)

请求前置的横切关注点：

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| 统一异常处理 | error_handler.py | `AppError` → HTTP 状态 + `{code, message, details}` 信封（main.py 已注册） | ✅ |
| 认证鉴权 | auth.py | JWT 认证、请求鉴权（当前由 deps.get_current_user 模拟） | ⬜ 预留 |
| API 限流 | rate_limit.py | 按用户 / IP / 全局维度限流（可复用 LLM 层 reserve/settle 思路） | ⬜ 预留 |

---

## Schema 与依赖注入

**代码**：`app/api/schemas/` + `app/api/deps.py` · **文档**：[routes.md](routes_doc/routes.md)（请求 / 响应模型与认证方式）

- **Schema**：`request.py` 定义 `SendMessageRequest` / `CreateSessionRequest`；`response.py` 定义 `CreateSessionResponse`。路由层只 import 使用，不在路由内定义
- **依赖注入**：`deps.py` 提供 `get_current_user`（模拟 Token 解析）与 `get_session_manager` / `get_context_manager` / `get_llm_service` / `get_tool_service` / `get_task_service` / `get_agent_params`（从 `container` 取单例，未初始化抛 `RuntimeError`）

---

## 典型调用链路

```text
客户端 → HTTP 请求 → main.py（CORS / SPA 回退 / error_handler 兜底）
    → 路由（chat / session，经 deps 注入用户身份 + 服务单例）
        → 服务层：SessionManager（会话验证）→ ContextManager（组装 messages）
        → TaskService.run_agent → ReActAgent 闭环（LLM 思考 ↔ 工具调用）
    → SSE 事件流回客户端（流结束保存 assistant 回复）
```

错误路径：任一层抛 `AppError` → error_handler 翻译为 `{code, message, details}` 信封返回客户端。

---

## 配置关联

- Agent 运行参数（`agent_max_iterations` / `llm_temperature` / `llm_max_tokens` / `agent_timeout` / `agent_max_context_rounds` / `max_context_tokens`）经 `container.agent_params` → `get_agent_params` 注入 chat 路由
- 并发约束（`agent_max_concurrent_tasks` / `agent_max_concurrent_tools`）作用于 TaskService / ToolService
- 全部配置项见 [config 文档](../config_doc/config.md)

---

## 相关文档

- [路由模块对外接口文档](routes_doc/routes.md)（端点契约 / 认证方式 / 异常契约）
- [中间件模块对外接口文档](middleware_doc/middleware.md)（统一错误信封 / auth / rate_limit）
- [架构设计](../architecture.md)（分层与演进路径）
- [应用层说明](../application_doc/README.md)（服务层，API 的下游）
- [集成层说明](../integration_doc/README.md)（LLM / 工具实现）
- [异常体系](../shared_doc/error_handling.md)（统一异常树，error_handler 的上游）
