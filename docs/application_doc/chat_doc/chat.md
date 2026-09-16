# ChatService 设计说明

> **对应代码**：`app/application/chat/chat_service.py`
> **文档定位**：聊天用例与单次运行生命周期的内部协作契约
> **更新日期**：2026-09-16
> 状态与映射见 [ALIGNMENT](../../ALIGNMENT.md)。

## 设计目标与边界

`ChatService` 位于 API 与 Agent 之间，负责会话预检、消息与上下文、运行身份、每请求 Agent、停止
以及最终答复提交。它不依赖 FastAPI `Request`/`Response`，也不定义 SSE 响应头、断连检测、error
帧或 `[DONE]`。这些传输责任保留在 `api/routes/chat.py`。

## 内部协作契约

| 入口 / 数据 | 行为 | 调用方责任 |
| --- | --- | --- |
| `prepare_message(...) -> ChatRun` | 响应头前完成 404/403、user 提交、上下文、run 登记及 Agent 创建 | 失败时不得构造 `StreamingResponse`；成功后必须关闭返回的 run |
| `ChatRun.events()` | 流首输出裁剪提示，再经 TaskService 转发 Agent 事件；只能消费一次 | 父级使用 `aclosing`，提前关闭时同步关闭子流 |
| `ChatRun.request_cancel()` | 置位本 run 的 cancel event | 只用于消费者关闭；公开 stop/断连仍按 session 取消 |
| `ChatRun.aclose()` | 幂等清 run 登记，提交非空 `agent.result` | 先关闭事件流再调用；保存失败向上暴露，不重放 Agent |
| `stop(session_id, user_id)` | 404/403 后取消该会话当时全部活动 run | API 保留既有 session 级停止契约 |
| `cancel_session(session_id)` | 将已确认的 HTTP 断连转换成 session 级取消 | 路由仅在首次发现断连时调用，随后继续排水 |

## 状态与执行流程

```text
HTTP 路由
  → prepare_message
      → 校验会话归属
      → 保存 user，取得 message_id
      → 构建上下文（历史只读 id < message_id）
      → 创建 run_id / cancel_event / run_stop / AgentContext / ReActAgent
  → ChatRun.events
      → TaskService.run_agent → Agent.run
  → 路由发送 error（如有）与 [DONE]（未断连且未提前关闭）
  → ChatRun.aclose
      → clear_cancel_event(run_id)
      → 非空 AgentResult 保存为 assistant
```

运行登记发生在返回 `StreamingResponse` 前，使 `/chat/stop` 能覆盖首事件前窗口。Agent 构造失败会
立即清登记。一个 run 自然结束只清自己的 `run_id`，同会话兄弟运行不受影响；会话 stop 和被动
断连仍置位该会话全部活动 run。

父子生成器在路由、`ChatRun.events` 和 `TaskService.run_agent` 三层显式 `aclosing`。聊天专用响应
还在 ASGI `__call__` 的外层 `finally` 关闭 body iterator 与 run，覆盖实际 `send()` 失败绕过 body
生成器收尾的路径。正常或可翻译异常路径由路由输出一次 `[DONE]`；客户端断连、
`CancelledError`、`GeneratorExit` 或消费者 `aclose` 不在关闭路径追加 SSE 帧。

## 消息与提交边界

- user 消息在上下文和运行登记前提交，保持长流开始前已接收消息可见。
- 历史快照只读取 `id < current_message_id`，排除当前消息和稍后提交的同会话兄弟消息；当前输入
  再追加一次，不按文本去重。消息主键缺失或非正数时在创建运行前失败。
- assistant 只保存 `agent.result.content.strip()` 非空的结果；取消、失败或降级不单独清除已接管成果。
- `[DONE]` 保持先于 assistant 提交。提交失败不触发 Agent、LLM 或工具重放，且 run 登记已经清理。

## 行为边界

| 场景 | 结果 |
| --- | --- |
| 会话不存在 / 越权 | 响应头前 404 / 403；零消息写入、零 run、零 LLM 调用 |
| 同会话多运行 | run_id、Agent、cancel event、run_stop 独立；stop 按 session 批量置位 |
| 首事件前运行失败 | 路由输出 error + `[DONE]`，清登记，不保存空 assistant |
| HTTP 断连 | 首次检测后 session 取消；停止推送，继续排水；ASGI 发送失败也由响应调用边界清理 |
| 消费者提前关闭 | 请求当前 run 取消，逐层关闭生成器；不输出 error / `[DONE]` |
| assistant 保存失败 | 异常向上暴露；run 已清；不自动重试或重跑业务 |

## 设计依据

采用显式运行 Owner 与确定性生成器关闭的取舍见
[CHAT-ADR-001](../../../adr/application/chat/2026-09-16-chat-run-owner.md)。当前消息重复问题与精确 ID
方案见 [CHAT-001](../../../issues/application/chat/2026-09-16-current-message-duplicated.md)。

## 验证入口

- `tests/unit/test_chat_service.py`：预检零副作用、消息 ID、同会话运行隔离、构造失败、幂等关闭与保存失败。
- `tests/unit/test_task_service.py`：提前 `aclose` 同步关闭 Agent 子流并释放信号量。
- `tests/integration/test_chat_flow.py`：正常 ReAct、stop、断连、首事件前失败、消费者关闭与当前消息唯一性。
- `tests/e2e/test_api.py`：真实 HTTP 401/404/403、SSE 和 stop 端点。

## 相关文档

- [应用层说明](../README.md)
- [路由契约](../../api_doc/routes_doc/routes.md)
- [SessionManager](../session_doc/session.md)
- [ContextManager](../context_doc/context.md)
- [TaskService](../task_doc/task.md)
