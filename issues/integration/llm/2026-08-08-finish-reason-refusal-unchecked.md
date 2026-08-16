# 不检查 finish_reason / refusal，截断与拒答被静默降级

> **状态**：✅ 已修复（2026-08-08，tool_calls 补充 2026-08-09）
> **优先级**：P1（中，合并前必修）
> **来源**：2026-08-07 structured 模块审核（问题 2）· 2026-08-16 从 structure.md 提取归档
> **涉及模块**：`app/integration/llm/structured.py`（`_classify_result` / `_try_extract` / `_fallback_extract`）· `app/integration/llm/streaming.py`（refusal 透传）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md)

---

## 问题描述

### 现象

`finish_reason="length"` 截断出半个 JSON → `json.loads` 失败 → **静默降级**；模型拒答 → content 为空 → **静默降级**——两类失败无区分、无日志。

### 影响

截断与拒答被静默吞掉：截断可扩 token 重试（有意义的缓解被丢弃）、拒答是安全策略信号（被当普通失败降级）；且模型输出被吞，调用方无法区分「三级耗尽」与「拒答」。

### 根因

解析前不检查 `finish_reason` / `refusal`——业界检查顺序固定为 `finish_reason` → `refusal` → `content` 存在性 → 解析 → schema 校验 → 业务校验，**任何解析动作之前必须先查 finish_reason**。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| **截断与拒答是两种必须显式区分的失败** | 检查顺序固定：`finish_reason` → `refusal` → `content` → 解析 → schema 校验 |
| **截断（length/max_tokens）** | 盲重试是反模式（Instructor PR #2232：截断输出拼回 prompt 重试，Gemini 烧掉 150 万 token）——本层内扩 max_tokens 重试至多 1 次；`length` 时 HTTP 200 也要自查 finish_reason |
| **拒答（refusal/content_filter）** | 拒答是「路由决策」而非「可捕获异常」——不强行 repair、不盲目降级重试，短路 + 记日志 + 返回可区分信号 |
| **refusal 字段** | OpenAI `message.refusal`（content 可置 null）；Anthropic `stop_reason:"refusal"`；DeepSeek **无 refusal 字段**（拒答只能靠「content 空 + finish_reason 异常」推断） |

> 调研对象：OpenAI/Anthropic/DeepSeek 官方语义、instructor、LiteLLM 归一化、openclaw、pi-refusal-guard（2026-08-08）。

---

## 修复方案（含决策取舍）

**决策**：解析前做三态检查（`_classify_result`），截断扩 token 重试、拒答短路抛 `StructuredRefusalError`、工具调用抛 `StructuredToolCallError`（均不进降级链）。

**修复要点**：

- **截断**（`finish_reason` ∈ `length`/`max_tokens`/`insufficient_system_resource`）：`_try_extract` 本层扩 max_tokens 重试 1 次；仍截断抛 `StructuredTruncationError`（短路返回 None，不降级）；
- **拒答/过滤**（`refusal` 字段 / `content_filter` / content 空且正常结束）：抛 `StructuredRefusalError`（不强行 repair），记区分日志；
- **数据透传**：`StreamResult` 加 `refusal` 字段；`parse_non_stream`/`parse_chunk` 提取（保留 None 与空串区分）；
- **tool_calls 补充（2026-08-09）**：`if not result.content:` 未排除 `finish_reason="tool_calls"` → 工具调用被误判拒答。定型为独立短路类别 `StructuredToolCallError`（模型已放弃输出 JSON，降级无意义，交回调用方按工具调用处理）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | `_classify_result` 三态分类 + `_raise_boundary` 统一短路（refusal/tool_calls/truncated）；新增 `StructuredRefusalError`/`StructuredToolCallError`/`StructuredTruncationError` | `test_generate_structured.py` 截断重试/拒答短路/工具调用短路用例 |
| `app/domain/ports/llm_gateway.py` / `app/integration/llm/streaming.py` / `app/integration/llm/llm_service.py` | `StreamResult.refusal` 字段透传（None/空串区分） | `test_streaming.py` refusal 透传用例 |

---

## 验证

- 截断扩 token 重试 1 次、拒答短路抛异常、工具调用短路抛异常，均不进降级链，记区分日志
- 全量测试通过（2026-08-08 修复 + 2026-08-09 tool_calls 补充时验证）

---

## 教训沉淀

- **任何解析动作之前必须先查 finish_reason**——截断/拒答/工具调用是 API 边界失败，必须显式区分处理，不能静默降级。
- **拒答是「路由决策」而非「可捕获异常」**：不 repair、不降级（同一段触发安全的输入喂给更宽松约束大概率同样拒答），短路 + 可区分信号。
- **截断绝不把半截 JSON 拼回 prompt 重试**（Instructor token 爆炸教训）——本层内扩 max_tokens 至多 1 次。
