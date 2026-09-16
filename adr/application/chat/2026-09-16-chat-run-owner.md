# 聊天用例与单次运行 Owner 边界

日期：2026-09-16。决定状态：已接受（R6 计划已获用户授权）。实现状态：已验证。
范围：`ChatService`、`ChatRun`、聊天路由及 Agent 流关闭链。关联：[R6 计划](../../../docs/todo.md#refactoring-plan)。

## 背景

聊天路由原先同时处理 HTTP、会话授权、用户消息、上下文、运行身份、Agent 创建、断连、运行清理
和 assistant 提交。业务编排无法脱离 FastAPI 测试，且异步生成器被消费者提前关闭时，外层退出
不会自动证明内层 Agent 流已同步关闭。

真实备选：

1. 保留路由编排，只提取局部函数。改动小，但 API 层仍直接创建 Agent 并拥有运行状态。
2. `ChatService` 返回一个裸异步生成器。接口简单，但运行在生成器首次迭代前已经登记；从未启动或
   提前关闭的裸生成器不能可靠执行函数体内的 `finally`，清理责任不完整。
3. `ChatService.prepare_message` 返回显式 `ChatRun` Owner。它持有唯一 run 身份、取消事件、Agent
   与幂等 `aclose`，路由只消费事件并在传输结束时关闭。该方案被采用。
4. 新增通用 AgentFactory。当前只有一个真实聊天创建方，且工厂没有独立状态或策略，因此否决。

工业参照采用 Python 官方
[`contextlib.aclosing`](https://docs.python.org/3/library/contextlib.html#contextlib.aclosing) 的确定性
异步生成器清理模式。Starlette 的 `StreamingResponse` 在 ASGI `send()` 失败时会从响应调用边界
退出，body 生成器内部的 `finally` 不能单独证明运行已关闭；因此聊天专用响应在 `__call__` 的
外层 `finally` 关闭 body iterator 与 `ChatRun`。

## 决定

- `ChatService` 在响应头前完成 404/403、用户消息提交、上下文构建与 run 登记，并负责 stop 的
  会话级授权和取消。
- 每次请求创建独立 `ChatRun`、`AgentContext`、`run_stop`、`cancel_event` 和 `ReActAgent`。
  `ChatRun.aclose` 只清理自己的 run，并提交 Agent 已接管的非空结果；重复关闭无第二次提交。
- 路由保留 `Request.is_disconnected`、异常到 SSE error、最终 `[DONE]`、响应头和
  `StreamingResponse`。断连仍取消同会话全部活动 run，停止推送后继续排水。
- 聊天专用响应包装器从 ASGI 调用边界兜底关闭 body iterator 与 `ChatRun`，覆盖实际发送失败
  绕过 body 生成器收尾的路径。
- 路由、ChatRun 与 TaskService 在各自父子生成器边界使用 `aclosing`；消费者提前关闭不额外产出
  error 或 `[DONE]`。
- assistant 提交保持在 `[DONE]` 之后；失败向上暴露但运行登记已释放，不启动重放或 fallback。

## 后果

- 收益：Application 用例可脱离 FastAPI 验证；HTTP 层不再直接接触 Agent 构造和运行参数；取消
  登记、结果提交与生成器关闭有明确 Owner。
- 代价：新增每次请求的 `ChatRun` 状态对象；维护者必须遵守“先关闭事件流，再 `aclose` 运行”的
  顺序，路由生成器与响应调用边界均依赖 `aclose` 幂等。
- 升级触发条件：出现第二种真实 Agent 创建用例且创建策略相同，才评估 AgentFactory；需要跨进程
  恢复聊天运行时，另行设计持久化状态和恢复协议，不能扩张当前进程内 Owner。

## 实现与验证

实现与测试入口见 [ChatService 组件说明](../../../docs/application_doc/chat_doc/chat.md)及
[ALIGNMENT](../../../docs/ALIGNMENT.md)。定向与全量结果回填于 [R6 计划](../../../docs/todo.md#refactoring-plan)。

## 关联记录

- [CHAT-001：当前用户消息重复进入模型上下文](../../../issues/application/chat/2026-09-16-current-message-duplicated.md)
- [REASON-003：取消信号语义](../../../issues/domain/reasoning/2026-08-30-cancel-event-semantics.md)
