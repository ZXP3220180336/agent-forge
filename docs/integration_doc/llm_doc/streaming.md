# StreamParser 设计文档

> **模块**：`app/integration/llm/streaming.py`
> **更新日期**：2026-08-16
> **职责**：流式 / 非流式 LLM 响应解析（逐 chunk 提取 reasoning / message / tool_calls / usage / refusal）
> **状态**：✅ 已实现
> **工业级对照**：增量累积 + 完成后解析（决策见 [设计决策](#设计决策)）

---

## 📋 目录

- [StreamParser 设计文档](#streamparser-设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [流式解析（逐 chunk 增量）](#流式解析逐-chunk-增量)
    - [纯函数无状态解析](#纯函数无状态解析)
    - [tool\_call 增量累积](#tool_call-增量累积)
    - [非流式解析](#非流式解析)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [ParsedChunk / ToolCallDelta — 数据结构](#parsedchunk--toolcalldelta--数据结构)
    - [parse\_chunk — 流式解析](#parse_chunk--流式解析)
    - [merge\_tool\_calls — 工具调用合并](#merge_tool_calls--工具调用合并)
    - [parse\_non\_stream — 非流式解析](#parse_non_stream--非流式解析)
  - [执行流程](#执行流程)
    - [流式解析流程](#流式解析流程)
    - [tool\_call 合并流程](#tool_call-合并流程)
    - [非流式解析流程](#非流式解析流程)
  - [对外接口](#对外接口)
  - [边界情况](#边界情况)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [问题记录](#问题记录)
  - [相关文档](#相关文档)

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
2. **finish_reason 独立提取**：不依赖 `delta` 是否为空——finish chunk 的 delta 可能为 None 或空对象，但 finish_reason 在 `choices[0]` 上，不能因 delta 为空而丢失（见 [问题记录](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)）
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

直接读取完整响应对象，产出统一 dict：`{"content", "finish_reason", "tool_calls", "usage", "refusal"}`。**空 choices 防护**：某些适配层/异常响应可能返回空 choices（无生成内容），直接 `response.choices[0]` 会抛裸 `IndexError`——返回空结果让调用方按「业务无结果」处理（见 [问题记录](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)）。**refusal 保留 None 与空串区分**：`or ""` 会把拒答的 None 抹成空串，下游无法判断「未拒答」与「拒答但文本为空」——直接透传原值。

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
4. **tool_call 片段缺字段**：`if tc.id` / `if tc.function`（`function` 可能为 None，缺失跳过）`if tc.function.name` 守卫——缺失字段保持空字符串，合并时按 index 兜底
5. **多工具调用交错**：`merge_tool_calls` 按 index 分组，交错增量正确合并
6. **空 tool_calls**：`merge_tool_calls([])` 返回空列表
7. **非流式无 usage**：`parse_non_stream` 对 `response.usage` 为 None 时返回 `usage: None`
8. **非流式空 choices**：`parse_non_stream` 对空 choices（适配层/异常响应）返回空结果而非抛 `IndexError`（见 [问题记录](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)）
9. **delta=None 的 finish chunk**：finish_reason 独立于 delta 提取，不因 delta 为空丢失（见 [问题记录](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)）
10. **usage 与空 delta 共存**：usage 独立于 choices/delta 提取，代理层违规共存时不静默丢弃（见 [问题记录](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)）
11. **非流式 refusal 空串 vs None**：直接透传原值，保留「未拒答」与「拒答但文本为空」的区分

---

## 测试状态

`tests/unit/test_streaming.py`（21 用例）：覆盖

- **parse_chunk**：content / reasoning / finish_reason / usage / tool_call 提取 / 字段缺失兜底 / 空 chunk / 混合 chunk（content + tool_calls）/ 漏洞回归（delta=None 丢 finish_reason、usage 与空 delta 共存）
- **merge_tool_calls**：单工具增量拼接 / 多工具交错 / 输出按 index 排序 / 缺 id 按 index 兜底 / id 覆盖策略 / 空列表
- **parse_non_stream**：content / tool_calls / usage / content 为 None 兜底

另被 `test_stream_rectify.py` 间接使用（通过 `async_generate` 走真实解析路径）。

---

## 设计决策

> 流式解析策略（tool_call 延迟组装 / 纯函数无状态 / usage 独立提取 / 不引入状态机引擎，Context → Decision → Consequences）已归档至 [ADR LLM-ADR-003](../../../adr/integration/llm/2026-08-01-streaming-parse-pure-function.md)。
---

## 问题记录

> 审核发现的问题（2026-08-07 / 08-09）已提取归档，完整生命周期（发现 → 分析 → 修复 → 验证 → 教训）见：

- [流式/非流式解析健壮性（finish_reason 丢失 / usage 丢弃 / tool_deltas 残留 / 空 choices 崩溃）](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)

## 相关文档

- [llm.md](llm.md)（LLM 层总览，含「流式解析：纯函数 vs 有状态类」对比）
- [streaming_rectifier.md](streaming_rectifier.md)（整流重试策略，调用方累积）
- [client.md](client.md)（ClientManager，流式响应来源的连接管理）
