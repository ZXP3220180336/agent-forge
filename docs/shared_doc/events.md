# 共享事件模块说明

> 对应代码：`app/shared/events.py`
> 状态：✅ 已实现

## 作用

LLM 层与 Agent 层共用的 SSE 事件类型与构建函数，确保两端产出的事件格式一致。

## 事件类型

| 类型 | 产出方 | 含义 |
| --- | --- | --- |
| `reasoning` | LLM 层 | 思考 token |
| `message` | LLM 层 | 回答 token |
| `tool_call` | Agent 层 | 工具调用通知 |
| `tool_result` | Agent 层 | 工具执行结果 |
| `done` | Agent 层 | Agent 完成 |
| `error` | 双端 | 异常 |
| `agent_info` | Agent 层 | 状态信息 |

## SSE 帧格式

所有事件序列化为 SSE 数据帧：`data: {json}\n\n`，`type` 字段区分事件类型（见上表）。帧由 [build_sse_event](../../app/shared/events.py) 统一构建（便捷构造器 `build_reasoning_event` / `build_message_event` / `build_tool_call_event` / `build_tool_result_event` / `build_done_event` / `build_error_event` / `build_info_event`）。

完整 ReAct 闭环示例：

```text
data: {"type": "agent_info", "content": "Agent 开始处理"}
data: {"type": "message", "content": "分析结果..."}
data: {"type": "tool_call", "content": "search", "params": {...}, "iteration": 1}
data: {"type": "tool_result", "content": "..."}
data: {"type": "done", "iterations": 2, "total_tokens": 1234}
data: [DONE]
```

## 相关文档

- [应用层说明](../application_doc/README.md)
- [领域层说明](../domain_doc/README.md)
- [路由模块](../api_doc/routes_doc/routes.md)（SSE 端点调用方：`chat/send`）
