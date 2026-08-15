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

## 相关文档

- [应用层说明](../application_doc/README.md)
- [领域层说明](../domain_doc/README.md)
