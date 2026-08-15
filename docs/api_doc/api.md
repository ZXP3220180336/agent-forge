# API 说明文档

> **更新日期**：2026-08-03
> **文档定位**：现有 REST API 端点、请求/响应模型、SSE 事件格式与认证方式。
> **当前已实现**：chat / session 路由。admin / agent / tool 路由为预留（见「预留路由」）。

---

## 📋 目录

- [基础信息](#基础信息)
- [认证方式](#认证方式)
- [会话管理 API](#会话管理-api)
- [聊天 API](#聊天-api)
- [SSE 事件格式](#sse-事件格式)
- [错误处理](#错误处理)
- [预留路由](#预留路由)

---

## 基础信息

- **Base URL**：`http://localhost:8000`
- **API 前缀**：`/api`
- **健康检查**：`GET /api/health` → `{"status": "ok", "version": "1.0.0"}`

---

## 认证方式

当前认证由 `app/dependencies.py` 的 `get_current_user()` **模拟实现**（非 JWT/OAuth）：

```
Authorization: Bearer <token>
→ 解析出 user_id = "user_" + token 前 8 字符
```

- 缺少 `Authorization` 头 → `401 未授权`
- 实际项目将替换为 JWT 验证

---

## 会话管理 API

### `POST /api/session/create` — 创建会话

请求：
```json
{ "system_prompt": "可选，默认'你是一个友好的AI助手'", "title": "可选，默认'新对话'" }
```
响应：
```json
{ "session_id": "uuid", "title": "新对话", "created_at": "ISO时间" }
```

### `GET /api/session/{session_id}` — 获取会话详情

响应：会话信息（id / user_id / system_prompt / created_at / message_count / total_tokens）

### `GET /api/session/{session_id}/history` — 获取会话历史

参数：`limit`（默认50，≤200）、`offset`（默认0）
响应：`{"session_id": "...", "messages": [{"role": "user|assistant", "content": "..."}]}`

### `GET /api/sessions` — 获取用户会话列表

响应：`{"sessions": [...]}`

### `DELETE /api/session/{session_id}` — 删除会话（软删除）

响应：`{"message": "会话已删除"}`

---

## 聊天 API

### `POST /api/chat/send` — 发送消息（流式 SSE）

请求：
```json
{ "session_id": "uuid", "message": "用户输入", "max_iterations": 10, "stream": true }
```

响应：`text/event-stream`，逐事件推送（见「SSE 事件格式」）。末尾 `[DONE]` 帧。

处理流程：
1. 会话验证与授权（404 会话不存在 / 403 无权访问）
2. 保存用户消息
3. ContextManager 构建 messages
4. TaskService.run_agent → ReActAgent 闭环
5. 逐事件推送 SSE
6. 流结束后保存 assistant 回复

### `POST /api/chat/stop` — 停止生成

响应：`{"message": "已发送停止信号"}`（当前为占位，未实际取消）

---

## SSE 事件格式

所有事件为 `data: {json}\n\n`，`type` 字段区分类型：

| type | 产出者 | 含义 | 关键字段 |
| --- | --- | --- | --- |
| `reasoning` | LLM 层 | 思考 token | content |
| `message` | LLM 层 | 回答 token | content |
| `error` | LLM/Agent | 错误 | content |
| `tool_call` | Agent | 工具调用通知 | content(工具名), params, iteration |
| `tool_result` | Agent | 工具执行结果 | content, tool, duration, iteration |
| `done` | Agent | 完成 | iterations, total_tokens |
| `agent_info` | Agent | 状态信息 | content |

示例（完整 ReAct 闭环）：
```
data: {"type": "agent_info", "content": "Agent 开始处理"}
data: {"type": "message", "content": "分析结果..."}
data: {"type": "tool_call", "content": "search", "params": {...}, "iteration": 1}
data: {"type": "tool_result", "content": "..."}
data: {"type": "done", "iterations": 2, "total_tokens": 1234}
data: [DONE]
```

---

## 错误处理

| 状态码 | 场景 |
| --- | --- |
| 401 | 未授权（缺少 Authorization 头） |
| 403 | 无权访问该会话（user_id 不匹配） |
| 404 | 会话不存在 |

---

## 预留路由

以下路由文件已创建但未实现（0 字节）：

| 路由 | 规划功能 |
| --- | --- |
| `admin.py` | 管理接口（系统状态、统计、运维） |
| `agent.py` | 异步任务受理（`POST /api/tasks/submit` + `GET /api/tasks/{id}`，承接 TaskService 调度） |
| `tool.py` | 工具管理（可基于 ToolService 的 stats 实现） |

---

## 相关文档

- [架构设计](../architecture.md)
- [路由模块](routes_doc/routes.md)（chat/session 路由详解）
- [中间件模块](middleware_doc/middleware.md)（认证/限流/错误处理）
- [agent 模块](../domain_doc/agent_doc/agent.md)
- [task 模块](../application_doc/task_doc/task.md)
- [tool 模块](../integration_doc/tools_doc/tools.md)
