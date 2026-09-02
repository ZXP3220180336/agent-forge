# 非流式 generate 丢弃 reasoning_content / has_reasoning（thinking 模型）

> **状态**：✅ 已修复（2026-09-02）
> **优先级**：P3（契约完整性缺口——当前领域层非流式消费方不读 thinking 内容，实际影响低；thinking 模型非流式场景真实存在）
> **来源**：2026-09-02 产物完整性审计发现（缺陷2：LLM 传输层 → 领域层产物字段缺失）
> **涉及模块**：`app/integration/llm/streaming.py`（`parse_non_stream`）· `app/integration/llm/llm_service.py`（`generate`）
> **关联文档**：[streaming.md](../../../docs/integration_doc/llm_doc/streaming.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## 问题描述

### 现象

非流式路径丢失 thinking 模型的思考内容：

1. `parse_non_stream` 只提取 `content / finish_reason / tool_calls / usage / refusal` 五个字段，**不读 `msg.reasoning_content`**；
2. `generate()` 填充 `StreamResult` 时同样不设 `reasoning_content` / `has_reasoning`——二者永远保持默认（`""` / `False`）。

### 影响

- **场景真实存在**：`generate_structured` 以 `reasoning` model_key 调用走非流式（test_generate_structured.py 的 `model_key="reasoning"` 用例即此形态）；thinking 模型非流式响应携带 `reasoning_content`，被静默丢弃；
- **契约缺口**：`StreamResult` 承诺 reasoning 字段（供编排层 thinking 回喂决策），非流式路径永远给空值——`has_reasoning` 无法表达「thinking 模型返回了空 reasoning」（与流式语义不一致）；
- **当前实际影响低**：领域层非流式消费方（结构化 dict / 简单任务）不读 thinking 内容；但契约缺失会在未来非流式 thinking 诊断场景静默丢信息。

### 根因

非流式解析（`parse_non_stream`）从流式解析（`parse_chunk`，已处理 reasoning）提炼时遗漏了 reasoning 字段——只对齐了 refusal 的「None/空串区分」，未对齐 reasoning 的「未返回/返回空」信号（`has_reasoning`）。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI / DeepSeek 非流式响应 | `message.reasoning_content` 与流式一致携带思考内容——流/非流式都是同一条 Message 契约 |
| 本项目流式 `has_reasoning` 语义 | 「字段被填充（含空串）」= thinking 信号，供编排层回喂（空 reasoning 也需回喂空串防 400）——非流式应同语义 |

**核心**：reasoning 是 Message 契约字段，流式 / 非流式解析应一致提取——`has_reasoning` 区分「未返回（chat 模型）」与「返回空（thinking）」对齐流式。

---

## 修复方案（含决策取舍）

**决策**：`parse_non_stream` 补提取 reasoning，`generate()` 补回填——最小垂直修复，不动领域层。

1. `parse_non_stream`：`reasoning = getattr(msg, "reasoning_content", None)`，返回 dict 增 `"reasoning_content": reasoning or ""` 与 `"has_reasoning": reasoning is not None`——`None`=未返回（chat 模型）、`""/非空`=返回（thinking 信号），空 choices 分支返回 `"" / False`；
2. `generate()`：`sr.reasoning_content = parsed.get("reasoning_content", "")`、`sr.has_reasoning = parsed.get("has_reasoning", False)`；
3. 返回 dict 增键同步 `parse_non_stream` docstring 与 streaming.md 输出描述。

**取舍**：`reasoning_content` 统一存 `""`（对齐 `StreamResult` str 类型默认），由 `has_reasoning` 承载存在性信号——与流式累积语义一致，不引入 `None` 分支复杂度。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/streaming.py` | `parse_non_stream` 补 `reasoning_content` / `has_reasoning`（含空 choices 分支 + docstring） | `tests/unit/test_streaming.py` 新增 2 例（非空 thinking / 空串 vs 未返回）+ 空 choices 全等断言补键 |
| `app/integration/llm/llm_service.py` | `generate()` 回填 `reasoning_content` / `has_reasoning` | `tests/unit/test_llm_service.py` 新增 1 例（generate 带回 thinking 字段） |
| `docs/integration_doc/llm_doc/streaming.md` | parse_non_stream 输出 dict / 流程 / 测试覆盖 / 用例数（22→24）/ 问题记录清单 | — |

---

## 验证

- `test_streaming.py` 新增：非流式 thinking 响应 → `reasoning_content` 提取 + `has_reasoning=True`；空串 `""` → `has_reasoning=True`（thinking 信号）vs 无字段（chat 模型）→ `False`——对齐流式语义；
- `test_llm_service.py` 新增：`generate()` 端到端 → `sr.reasoning_content == "思考"`、`sr.has_reasoning is True`；
- 相关文件 96 passed（test_streaming / test_llm_service / test_generate_structured / test_streaming_rectifier）；`verify_alignment` 通过。

---

## 教训沉淀

- **从流式提炼非流式解析器时，Message 契约字段要全量对齐**：提炼 `parse_non_stream` 时对齐了 refusal 的「None/空串区分」，漏了 reasoning 的「未返回/返回空」信号——同一 Message 契约的字段语义应在流/非流式两侧一致，提炼后逐字段对账；
- **契约字段缺失在「当前无消费方」时仍是缺口**：`StreamResult` 承诺了 reasoning，非流式永远给空值即契约撒谎——先按契约完整性修复，再等真实消费需求。
