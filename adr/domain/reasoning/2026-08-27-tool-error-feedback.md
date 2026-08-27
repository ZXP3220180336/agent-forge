# 工具失败回喂 + 无效工具名处理

> 日期：2026-08-27 ｜ 层级：domain/reasoning + integration/tools

## Context

- 对标文档 [react_benchmark.md](../../../docs/domain_doc/reasoning_doc/react_benchmark.md) 确认核心必备 #5（工具异常回喂，P0）与 #6（无效工具名处理）缺失：工具失败时 `ToolResult.content=""`，`execute_tool_calls` 只回喂 `content[:2000]` 空串——模型看不到失败原因无法自愈；同时 `_tool_call_records` 只记 `result: content`，`error` 未进证据链记录。
- 无效工具名（NOT_REGISTERED）同根因：失败 `error` 不进 messages。
- 工业级参照（OpenAI `failure_error_function` / LangChain `InvalidTool` / SMOL 错误回喂）：工具/解析错误一律转文本回喂模型让模型自愈。

## Decision

1. **回喂模型**：成功回喂 `content`，失败回喂 `str(exec_result)`（`ToolResult.__str__` 失败返回 `错误: <error>`）——模型可感知失败原因并自愈。
2. **证据链记录**：`_tool_call_records` 新增 `error` / `error_code` 字段（`error_code.value`，无系统码为 `None`）——根因报告可见「哪个工具失败、为什么失败、错误分类」。
3. **SSE 事件**：`build_tool_result_event` 同步改用 `feedback`（前端可见失败原因）。
4. **无效工具名不写额外分支**：NOT_REGISTERED 失败自然走失败回喂，模型可见「工具未注册」。
5. **顺带修复 AGENT-001**：`except json.JSONDecodeError, KeyError:` → `except (json.JSONDecodeError, KeyError):` 显式元组（同函数内）。
6. **参数 JSON 解析失败降级**：解析失败不执行工具（避免空参执行的错误掩盖 / 副作用），构造失败 `ToolResult`（`error="参数 JSON 解析失败: ..."`, `error_code=JSON_PARSE`）走失败回喂分支——模型可见原因自纠，错误码进证据链。
7. **截断标记**：工具结果回喂截断（tool 消息 2000 / SSE 200）时追加 `[结果已截断]` 标记（预留标记长度保证不超限）——模型可知结果不完整，而非误以为完整。

## Consequences

- ✅ 模型自愈闭环：失败 / 无效工具 / 解析失败的错误文本回喂，下一轮 LLM 可纠正。
- ✅ 证据链完整：`outcome.tool_calls` 含 `error` / `error_code`，供根因报告归因。
- ✅ 新增 4 单元测试（失败回喂 / 证据链记录 / 无效工具名 / 解析失败不执行工具）。
- ⚠️ 回喂截断 2000 字符沿用（失败文本通常短，完整进模型）；成功路径行为不变。
- 📌 `error_code` 进证据链不进回喂（模型收到文本已足够，枚举值留审计 / 证据链）。
