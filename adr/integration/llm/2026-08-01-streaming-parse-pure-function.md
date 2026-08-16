# 流式解析策略：纯函数无状态 + tool_call 延迟组装 + usage 独立提取

> **状态**：✅ 已采纳
> **决策日期**：2026-08-01（StreamParser 初建确立）
> **涉及模块**：`app/integration/llm/streaming.py`（`StreamParser`）
> **关联文档**：[streaming.md](../../../docs/integration_doc/llm_doc/streaming.md) · [streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md)

---

## Context

- 流式响应逐 chunk 到达，`delta.tool_calls[i].function.arguments` 按 token 碎片到达——中途片段如 `{"na` 几乎永远不是合法 JSON，立即 `json.loads` 必然抛 `JSONDecodeError`。
- 解析器形态有两种选择：**有状态类**（`parser.feed(chunk)` → `parser.result()`，内部缓冲）vs **纯函数**（`parse_chunk(chunk)` → 增量，调用方累积）。
- `usage` 只在**最后一个 chunk** 返回（需请求 `stream_options: {include_usage: true}`），该 chunk 通常 `choices` 为空、只有 `usage`。
- 工业界（[vLLM #44873](https://github.com/vllm-project/vllm/issues/44873)）对超大规模场景提出 **TokenIDScanner → IncrementalLexer → StreamingParserEngine** 状态机引擎。

## Decision

**`StreamParser` 采用纯函数无状态解析，tool_call 参数延迟组装，usage/finish_reason 独立提取，不引入状态机引擎。**

1. **tool_call 增量不立即解析**：`delta.tool_calls[i].function.arguments` 静默累积（concat 到字符串），**直到 `finish_reason` 到达才 flush** 为完整 JSON——不基于中途 JSON 可解析性做提前判定（安全考量）。
2. **纯函数无状态**：`parse_chunk(chunk)` 返回 `ParsedChunk` 增量，无副作用——测试友好（mock 一个 chunk 断言输出）；与整流重试契合（无状态天然幂等，整流重新迭代无需清空缓冲）；与事件层解耦（调用方决定何时转 SSE 事件）。
3. **多工具按 index 合并**：`merge_tool_calls` 用 `acc: dict[int, dict]` 按 `ToolCallDelta.index` 分组累积，按 index 排序输出；缺失 ID 时按 index 合成稳定 ID（对齐工业实现）。
4. **usage 独立提取**：入口判断 `if not chunk.choices or not chunk.choices[0].delta` 精确命中「无内容增量」形态，此时只读 usage——「usage 只信最终 chunk，不期待每个 delta 都有」。
5. **不引入状态机解析引擎**：单一 provider（OpenAI 兼容端点）无多 provider 适配需求；逐个 chunk 处理天然 chunk-size 无关（不假设「一 chunk 一 token」）；单个 `parse_chunk` 同时提取 reasoning/content/tool_calls，已是「单状态机统一处理」。

## Consequences

- **正面**：可测试性高（无状态纯函数）；并发安全（无共享缓冲）；整流重试天然幂等；chunk-size 无关；单一 provider 场景简单、无两遍解析脆弱性。
- **负面**：调用方需自己写 `tool_deltas.extend(...)` 累积循环（纯函数设计的**显式化代价**，换来灵活性与可测试性）；若未来支持 Anthropic / Gemini / Responses API 等多 provider，才需引入适配器层（归一化为通用事件）与严格/宽松解析模式——当前决策不覆盖，届时需重新评估。
