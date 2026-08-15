# StreamParser 设计文档

> **模块**：`app/integration/llm/streaming.py`
> **职责**：流式 / 非流式 LLM 响应解析（逐 chunk 提取 reasoning / message / tool_calls / usage / refusal）
> **工业级对照**：增量累积 + 完成后解析（见「设计决策·Q1/Q2」）

---

## 目录

- [设计目标](#设计目标)
- [核心概念解释](#核心概念解释)
  - [流式解析（逐 chunk 增量）](#流式解析逐-chunk-增量)
  - [纯函数无状态解析](#纯函数无状态解析)
  - [tool_call 增量累积](#tool_call-增量累积)
  - [非流式解析](#非流式解析)
- [架构总览](#架构总览)
- [组件详解](#组件详解)
  - [ParsedChunk / ToolCallDelta — 数据结构](#parsedchunk--toolcalldelta--数据结构)
  - [parse_chunk — 流式解析](#parse_chunk--流式解析)
  - [merge_tool_calls — 工具调用合并](#merge_tool_calls--工具调用合并)
  - [parse_non_stream — 非流式解析](#parse_non_stream--非流式解析)
- [执行流程](#执行流程)
  - [流式解析流程](#流式解析流程)
  - [tool_call 合并流程](#tool_call-合并流程)
  - [非流式解析流程](#非流式解析流程)
- [设计决策](#设计决策)
  - [Q1: 为什么 tool_call 增量不立即解析 JSON？](#q1-为什么-tool_call-增量不立即解析-json)
  - [Q2: 为什么用「纯函数 + 调用方累积」而非「有状态解析器」？](#q2-为什么用纯函数--调用方累积而非有状态解析器)
  - [Q3: `merge_tool_calls` 如何按 index 合并多个工具调用？](#q3-merge_tool_calls-如何按-index-合并多个工具调用)
  - [Q4: usage 为什么只在「无 choices」的 chunk 读？](#q4-usage-为什么只在无-choices-的-chunk-读)
  - [Q5: 与工业级「状态机解析引擎」的差距？](#q5-与工业级状态机解析引擎的差距)
- [对外接口](#对外接口)
- [边界情况](#边界情况)
- [测试状态](#测试状态)
- [改造记录与工业实践](#改造记录与工业实践)

---

## 设计目标

1. **纯函数无状态**：`parse_chunk` 每次返回独立的 `ParsedChunk`，不维护内部缓冲——调用方决定如何累积、何时消费
2. **与事件层解耦**：解析器不知道 `build_message_event()` 等事件构造函数的存在，只产出数据对象
3. **流式 / 非流式统一**：`parse_non_stream()` 复用同一套数据结构，两通道产出一致
4. **中途容忍非法 JSON**：tool_call 参数增量是 JSON 碎片，解析器只存增量字符串，不尝试解析中间态（完成后才组装）

---

## 核心概念解释

### 流式解析（逐 chunk 增量）

OpenAI SDK 的流式响应是**逐 chunk** 到达的（每个 chunk 携带一小段增量）。`parse_chunk(chunk)` 负责把单个 chunk 拆成结构化增量：

```
chunk → ParsedChunk
    ├─ reasoning_token    推理过程片段（如 DeepSeek-R1）
    ├─ message_token      回复文本片段
    ├─ finish_reason      停止原因（stop / length / tool_calls）
    ├─ usage              Token 用量（最后一个 chunk）
    ├─ refusal            拒答形态（delta.refusal 到达）
    └─ tool_call_deltas   工具调用增量列表
```

每次调用产出**独立对象**，不累积内部状态——累积由调用方（如 `StreamingRectifier`）负责。

### 纯函数无状态解析

`parse_chunk` / `merge_tool_calls` / `parse_non_stream` 均为**静态方法**，输入对象、输出数据，无共享缓冲。这带来：

- **测试友好**：直接 mock 一个 chunk，断言输出；无需 reset 状态
- **整流重试幂等**：整流重试重新迭代时，每次迭代的增量独立，无残留缓冲
- **并发安全**：无共享可变状态，天然线程/异步安全

### tool_call 增量累积

`parse_chunk` 只产出 `ToolCallDelta` 增量（`index` / `id` / `function_name` / `function_arguments`），**累积与合并由调用方完成**——这是「纯函数」设计的显式化：解析器不管累积时机，调用方决定何时合并。

### 非流式解析

`parse_non_stream(response)` 直接读取完整响应对象（非流式接口一次返回），产出与「流式合并后」一致的 dict。两通道最终数据形态统一，调用方无需区分来源。

---

## 架构总览

```
调用方（StreamingRectifier / async_generate / generate）
        │
        ▼
    StreamParser（纯函数静态类，无实例状态）
    ├── parse_chunk(chunk) ──────► ParsedChunk（单 chunk 增量）
    │        └─ ToolCallDelta（工具调用增量，调用方累积）
    ├── merge_tool_calls(deltas) ──► list[dict]（OpenAI 格式 tool_calls）
    └── parse_non_stream(response) ► dict（统一数据形态）

    # 数据流：
    #   流式：chunk 流 → parse_chunk → 增量累积 → 流结束 merge_tool_calls
    #   非流式：完整 response → parse_non_stream → 统一 dict
```

**分层**：

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 数据层 | `ParsedChunk` | 单 chunk 解析结果（增量数据对象） |
| 数据层 | `ToolCallDelta` | 工具调用增量片段（index 分组） |
| 解析层 | `parse_chunk()` | 逐 chunk 拆解为结构化增量 |
| 合并层 | `merge_tool_calls()` | 增量列表合并为完整 tool_calls |
| 解析层 | `parse_non_stream()` | 非流式完整响应解析 |

---

## 组件详解

### ParsedChunk / ToolCallDelta — 数据结构

```python
@dataclass
class ParsedChunk:
    """单个 chunk 的解析结果。"""
    reasoning_token: str | None = None   # 推理过程片段（如 DeepSeek-R1）
    message_token: str | None = None     # 回复文本片段
    finish_reason: str | None = None     # 停止原因（stop / length / tool_calls）
    usage: dict | None = None            # Token 用量（最后一个 chunk）
    refusal: str | None = None           # 拒答形态（delta.refusal）
    tool_call_deltas: list[ToolCallDelta] | None = None  # 工具调用增量

@dataclass
class ToolCallDelta:
    """工具调用的增量片段。"""
    index: int            # 工具索引（多工具时区分）
    id: str = ""          # 工具 call ID
    function_name: str = ""
    function_arguments: str = ""   # 参数 JSON 增量
```

两个数据对象均为**纯数据**：无行为、无累积逻辑，只承载解析中间产物。

### parse_chunk — 流式解析

```python
@staticmethod
def parse_chunk(chunk: Any) -> ParsedChunk:
```

**提取顺序**（与数据语义相关）：

1. **usage 独立提取**：不依赖 `choices` 为空——usage-only chunk 的 choices 通常为空，但某些代理/适配层可能在带 delta 的 chunk 上也附带 usage，不应静默丢弃
2. **finish_reason 独立提取**：不依赖 `delta` 是否为空——finish chunk 的 delta 可能为 None 或空对象，但 finish_reason 在 `choices[0]` 上，不能因 delta 为空而丢失（2026-08-07 修复）
3. **无内容增量即返回**：`not chunk.choices or not chunk.choices[0].delta` → 到此为止（usage-only / finish-only chunk）
4. **有 delta** → 提取 `reasoning_content` / `content` / `refusal` / `tool_calls`（字段缺失用 `hasattr` 守卫，不同 provider/模型的 delta 字段集不同，缺失不崩溃）

### merge_tool_calls — 工具调用合并

```python
@staticmethod
def merge_tool_calls(deltas: list[ToolCallDelta]) -> list[dict[str, Any]]:
```

用 `acc: dict[int, dict]` 按 `ToolCallDelta.index` 分组累积，`id` / `function_name` / `function_arguments` 各字段跨增量 concat；最后按 index 排序输出。输出为 OpenAI 格式：`[{"id", "type": "function", "function": {"name", "arguments"}}]`。

### parse_non_stream — 非流式解析

```python
@staticmethod
def parse_non_stream(response: Any) -> dict[str, Any]:
```

直接读取完整响应对象，产出统一 dict：`{"content", "finish_reason", "tool_calls", "usage", "refusal"}`。**空 choices 防护**：某些适配层/异常响应可能返回空 choices（无生成内容），直接 `response.choices[0]` 会抛裸 `IndexError`——返回空结果让调用方按「业务无结果」处理（2026-08-09 修复）。**refusal 保留 None 与空串区分**：`or ""` 会把拒答的 None 抹成空串，下游无法判断「未拒答」与「拒答但文本为空」——直接透传原值。

---

## 执行流程

### 流式解析流程

```
chunk 流（async for）
  └─ parse_chunk(chunk)
       ├─ usage 提取（独立于 choices）
       ├─ finish_reason 提取（独立于 delta）
       ├─ 无内容增量 → 返回 ParsedChunk(usage/finish_reason)
       └─ 有 delta → reasoning / content / refusal / tool_call_deltas
```

### tool_call 合并流程

```
调用方累积 tool_deltas：list[ToolCallDelta] = []
  async for chunk → parse_chunk → tool_call_deltas 追加
流结束（finish_reason）→ merge_tool_calls(tool_deltas)
  → 按 index 分组 concat arguments → 按 index 排序 → OpenAI 格式列表
```

### 非流式解析流程

```
parse_non_stream(response)
  ├─ 空 choices → 返回空结果（业务无结果，不抛 IndexError）
  └─ 有 choices → 读 msg.content / tool_calls / usage / refusal → 统一 dict
```

---

## 设计决策

### Q1: 为什么 tool_call 增量不立即解析 JSON？

**因为中途的 JSON 碎片几乎永远不是合法 JSON。**

`delta.tool_calls[i].function.arguments` 按 token 流式到达，前几个片段如 `{"na` 无法 `json.loads`。若解析器尝试立即解析，会抛出 `JSONDecodeError` 或在 UI 上展示残缺结构。

工业级语义（参考 [go-ai openAICompatStream](https://raw.githubusercontent.com/digitallysavvy/go-ai/refs/tags/v0.4.0/pkg/providerutils/streaming/openai_compat_stream.go)、[DataDog dd-trace-js #8227](https://github.com/DataDog/dd-trace-js/pull/8227)）：

- **tool_call 参数增量静默累积**（concat 到字符串），**不解析**
- **直到 `finish_reason` 到达才 flush** 为完整 JSON——这同时是安全考量：不基于中途 JSON 可解析性做提前判定

本项目 `merge_tool_calls` 按 index 分组 concat `function.arguments`，正是「累积 → 完成后组装」的工业语义。解析器本身不做 `json.loads`，天然避免了流式参数当完整 JSON 解析的坑。

### Q2: 为什么用「纯函数 + 调用方累积」而非「有状态解析器」？

| 维度 | 纯函数（当前） | 有状态类 |
| --- | --- | --- |
| 测试 | 输入 chunk → 输出 ParsedChunk，无副作用 | 需要 reset 状态 |
| 并发安全 | 天然安全（无共享缓冲） | 需注意状态清理 |
| 使用方式 | `parse_chunk(chunk)` 返回增量 | `parser.feed(chunk)` → `parser.result()` |
| 灵活性 | 高（调用方控制累积时机） | 中（内部缓冲） |

选择理由：

- **测试友好**：直接 mock 一个 chunk，断言输出；有状态解析器每次测试要 reset
- **与整流重试契合**：`StreamingRectifier`（见 [streaming_rectifier.md](streaming_rectifier.md)）的整流重试会重新迭代，每次迭代的增量是独立的——无状态解析器天然幂等，有状态解析器需在整流前清空缓冲
- **与事件层解耦**：调用方决定何时把增量转成 SSE 事件、何时合并 tool_call

**代价**：调用方需要自己写 `tool_deltas.extend(...)` 循环——这是「纯函数」设计的显式化，换来灵活性与可测试性。

### Q3: `merge_tool_calls` 如何按 index 合并多个工具调用？

一个响应可能包含**多个**工具调用，增量交错到达：

```
index=0: {"na                    index=1: {"query
index=0: me": "张三"}             index=1: "ery": "天气"}
```

`merge_tool_calls` 用 `acc: dict[int, dict]` 按 `ToolCallDelta.index` 分组累积，`id`/`function_name`/`function_arguments` 各字段跨增量 concat；最后按 index 排序输出。这样：

- **多工具独立累积**：每个 index 一个累加条目，互不干扰
- **按 index 排序**：输出 `[acc[0], acc[1], ...]`，保持稳定顺序
- **兼容缺失 ID**：`id` 为空字符串时仍按 index 兜底——与工业实现「缺失 tool id 时按 index 合成稳定 ID」一致（参考 [llm-stream-assemble](https://github.com/01laky/llm-stream-assemble)）

### Q4: usage 为什么只在「无 choices」的 chunk 读？

OpenAI 流式响应的 usage 只在**最后一个 chunk** 返回（需请求 `stream_options: {include_usage: true}`）。该 chunk 通常 `choices` 为空、只有 `usage` 字段。

`parse_chunk` 的入口判断 `if not chunk.choices or not chunk.choices[0].delta` 精确命中这一形态：无内容增量时只读 usage。这与工业级「usage 只信最终 chunk，不期待每个 delta 都有」一致。

### Q5: 与工业级「状态机解析引擎」的差距？

工业界（[vLLM #44873](https://github.com/vllm-project/vllm/issues/44873)）对超大规模场景提出 **TokenIDScanner → IncrementalLexer → StreamingParserEngine** 的状态机引擎，解决：

- **O(n) 复杂度**：每个 token 只处理一次（部分 naive 解析器对多 token chunk 退化为 O(n²)）
- **chunk-size 无关**：speculative decoding 下 chunk 可能含多个 token，很多解析器假设"一 chunk 一 token"会出错
- **统一 reasoning/tool 解析**：避免两遍解析（先 reasoning 后 tool）的脆弱性

**本项目为何不需要**：

- **单一 provider**（OpenAI 兼容端点），无多 provider 适配需求
- **逐个 chunk 处理**，天然 chunk-size 无关（不假设一 chunk 一 token）
- 单个 `parse_chunk` 同时提取 reasoning/content/tool_calls，已是「单状态机统一处理」

若未来支持 Anthropic / Gemini / Responses API 等多 provider，才需引入适配器层（归一化为 `text.delta` / `tool_call.args.delta` 等通用事件）与严格/宽松解析模式。

---

## 对外接口

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `parse_chunk(chunk) -> ParsedChunk` | 同步静态 | 解析单个流式 chunk，产出增量数据 |
| `merge_tool_calls(deltas) -> list[dict]` | 同步静态 | 将 ToolCallDelta 列表合并为完整 tool_calls |
| `parse_non_stream(response) -> dict` | 同步静态 | 解析非流式完整响应，产出统一 dict |

---

## 边界情况

1. **无 choices 的 chunk**：只有 usage（最后一个 chunk）→ 返回 `ParsedChunk(usage=...)`
2. **delta 为空**：`not chunk.choices[0].delta` → 检查 usage，无则空 ParsedChunk
3. **字段缺失**：`hasattr(delta, "content")` 守卫——不同 provider/模型的 delta 字段集不同，缺失不崩溃
4. **tool_call 片段无 id / name**：`if tc.id` / `if tc.function.name` 守卫，缺失时保持空字符串，合并时按 index 兜底
5. **多工具调用交错**：`merge_tool_calls` 按 index 分组，交错增量正确合并
6. **空 tool_calls**：`merge_tool_calls([])` 返回空列表
7. **非流式无 usage**：`parse_non_stream` 对 `response.usage` 为 None 时返回 `usage: None`
8. **非流式空 choices**：`parse_non_stream` 对空 choices（适配层/异常响应）返回空结果而非抛 `IndexError`（2026-08-09 修复）
9. **delta=None 的 finish chunk**：finish_reason 独立于 delta 提取，不因 delta 为空丢失（2026-08-07 修复）
10. **usage 与空 delta 共存**：usage 独立于 choices/delta 提取，代理层违规共存时不静默丢弃（2026-08-07 修复）
11. **非流式 refusal 空串 vs None**：直接透传原值，保留「未拒答」与「拒答但文本为空」的区分

---

## 测试状态

`tests/unit/test_streaming.py`（21 用例）：覆盖

- **parse_chunk**：content / reasoning / finish_reason / usage / tool_call 提取 / 字段缺失兜底 / 空 chunk / 混合 chunk（content + tool_calls）/ 漏洞回归（delta=None 丢 finish_reason、usage 与空 delta 共存）
- **merge_tool_calls**：单工具增量拼接 / 多工具交错 / 输出按 index 排序 / 缺 id 按 index 兜底 / id 覆盖策略 / 空列表
- **parse_non_stream**：content / tool_calls / usage / content 为 None 兜底

另被 `test_stream_rectify.py` 间接使用（通过 `async_generate` 走真实解析路径）。

---

## 改造记录与工业实践

> 本节以审核发现的问题为主线，记录**问题 → 修复 → 工业对照**。

### 2026-08-07 审核修复

| 问题 | 修复前 | 修复后 |
| --- | --- | --- |
| **delta=None 丢 finish_reason** | `parse_chunk` 原守卫 `not chunk.choices[0].delta` 在 delta=None 的 finish chunk 上提前 return，丢失 finish_reason | 重构为 finish_reason 独立提取，不依赖 delta 是否为空 |
| **usage 与空 delta 共存时静默丢弃** | usage 提取依赖 choices 为空 | usage 独立于 choices/delta 提取，代理层违规共存时不静默丢弃 |
| **整流 continue 前未清空 tool_deltas** | 整流重试 `continue` 前未显式清空 | 防御性清空，防未来重构携带脏数据 |

### 2026-08-09 修复

| 问题 | 修复前 | 修复后 |
| --- | --- | --- |
| **非流式空 choices 抛 IndexError** | `parse_non_stream` 直接 `response.choices[0]`，空 choices 抛裸 IndexError | 空 choices 防护：返回空结果让调用方按「业务无结果」处理 |

### 工业实践对照

| 议题 | 工业级做法 | 本项目立场 |
| --- | --- | --- |
| **tool_call 增量解析** | 增量 concat 累积、`finish_reason` 后才 flush（go-ai / dd-trace-js） | `merge_tool_calls` 按 index 分组 concat，完成后组装 |
| **缺失 tool id** | 按 index 合成稳定 ID（llm-stream-assemble） | `id` 为空时按 index 兜底 |
| **状态机解析引擎** | vLLM TokenIDScanner → IncrementalLexer → StreamingParserEngine | 单一 provider 场景不需要；未来多 provider 才引入适配器层 |

---

## 相关文档

- [llm.md](llm.md)（LLM 层总览，含「流式解析：纯函数 vs 有状态类」对比）
- [streaming_rectifier.md](streaming_rectifier.md)（整流重试策略，调用方累积）
- [client.md](client.md)（ClientManager，流式响应来源的连接管理）
