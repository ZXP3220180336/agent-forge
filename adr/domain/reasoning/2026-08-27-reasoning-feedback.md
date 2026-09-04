# reasoning_content 回喂策略（DeepSeek V4 必须回喂 + has_reasoning 防御）

> 日期：2026-08-27 ｜ 层级：domain/reasoning + integration/llm

## Context

- 对标文档曾把「reasoning_content 回喂」列为 P2 问题（"OpenAI 兼容 API 不接受该字段"）——经联网调研确认是**误用 OpenAI o1 规则的错误外推**。
- 项目 main = `deepseek-v4-flash`（DeepSeek V4 思考模式）。DeepSeek V4 规则：请求携带 `tools` 时历史 assistant 消息**必须回传 reasoning_content**，否则下轮 HTTP 400（LangChain PR #35620 / Spring AI #5027 / n8n 均为此修复）。OpenAI o1 的「不回喂」规则不适用于本项目协议。
- 真实边界：原 `if full_reasoning:` 只在非空时加字段——thinking 模式某轮 reasoning 为空时字段缺失会 400。

## Decision

1. **回喂语义**：DeepSeek V4 thinking + tools 下，assistant 消息必须原样回喂 `reasoning_content`。
2. **`has_reasoning` 信号**：`StreamResult` / `ParsedChunk` 新增 `has_reasoning` 标记——`reasoning_content` 字段被填充（`is not None`，含空串）即置位，区分「未发送（None）」与「空 thinking 块（空串）」；react.py 回喂条件改为 `if full_reasoning or has_reasoning`（空串也回喂，字段始终存在）。
3. **非流式回喂同构**（2026-09-04 更新）：ReAct 增加 `stream_mode=False` 非流式通道后，`generate()` 的 `parse_non_stream` 同样产出 `reasoning_content` / `has_reasoning`（字段与流式整流合并结果同构），回喂语义**两通道一致**——原「非流式路径不处理」已随 [react-stream-channel](2026-09-04-react-stream-channel.md) 落地而失效。

## Consequences

- ✅ chat 模型（未发送 reasoning_content）不置位，不带多余字段，无兼容风险。
- ✅ thinking 模型空 reasoning 也回喂空串，堵住 400 边界。
- ⚠️ 依赖 SDK 对空 reasoning 块返回 `""` 而非 `None`（多数实现如此）；若某代理返回 `None`，信号退化为「有内容才回喂」= 原行为，无害。
- ✅ 新增测试：`parse_chunk` has_reasoning 三态 + react 回喂两用例。
