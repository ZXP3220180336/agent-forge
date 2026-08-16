# LLM-004 空 content 一律归类 refusal，适配层空响应被误判为安全拒答

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 4）
> **涉及模块**：`app/integration/llm/structured.py`（`_classify_result` / `_try_extract` / `_fallback_extract`）· `app/integration/llm/streaming.py`（`parse_non_stream` 空 choices 契约）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md) · [streaming.md](../../../docs/integration_doc/llm_doc/streaming.md)

---

## 问题描述

### 现象

`structured.py` 的 `_classify_result` 中 `if not result.content: return "refusal"` 把一切「content 空」归类为拒答。而 `parse_non_stream` 对空 choices 返回 `content=""`（契约语义是「业务无结果」）——适配层空响应（无 refusal、无 finish_reason、content 空）被误判为**模型安全拒答**，抛 `StructuredRefusalError`。

### 影响

- 适配层空响应/流中断无结果 → 被短路为「安全拒答」抛给调用方，而非「业务无结果」触发三级降级；
- 调用方需区分「三级耗尽」与「拒答」，误判导致差异化处理走错分支（拒答通常需要安全兜底/文案）。

### 根因

用「content 空」推断拒答——工业界明确不推荐：content 空 ≠ 拒答，可能是适配层空响应、reasoning_content 占用输出、模型空回等「生成失败」。

---

## 工业级参照

| 参照 | 做法 | 对应本项目 |
| --- | --- | --- |
| OpenAI Chat Completions `finish_reason` | `content_filter` 是拒答显式信号；`stop`/`length`/`tool_calls` 各有语义；「200 OK 但内容空」是应用层失败而非传输成功 | `content_filter` 已在 `_REFUSAL_REASONS` 捕获（保留）；空 content 不应单独推断拒答 |
| 模型自身拒答形态 | 模型拒答通常产出**完整礼貌拒绝文本**（`stop` + 有内容），而非空 content | 空 content 是异常形态，更可能是生成失败 |
| Anthropic Messages | `refusal` 是显式 `stop_reason`；拒绝与正常结束靠 stop_reason 区分 | 本项目 refusal 字段 / `content_filter` 是显式信号（保留） |
| read.markets 代码库 | 空 content 视为**生成失败** → 触发 retry/skip（不是拒答）；reasoning 不可作为用户可见输出兜底 | 空 content 应触发降级（放宽约束重试），而非短路拒答 |

**核心**：拒答应基于**显式信号**（`refusal` 字段 / `finish_reason=content_filter`），不能靠「content 空」推断。

---

## 修复方案（含决策取舍）

**决策**：`_classify_result` 的 content 空分支区分 finish_reason——**无 finish_reason + content 空 → 新增 `"empty"` 分类**（返回 None 触发降级）；**有 finish_reason（如 stop）+ content 空 → 保持 `"refusal"`**（保留 DeepSeek 无 refusal 字段的拒答形态识别）。

**取舍理由**：

1. 精确对齐报告建议（「无 refusal、无 finish_reason、content 空」→ empty → 降级）；
2. 保留 DeepSeek 拒答识别（finish_reason=stop + content 空仍是拒答形态，现有测试不破坏）；
3. 工业界「显式信号优先」——只有显式拒答信号（refusal / content_filter / 有 finish_reason 的空回）才判拒答；finish_reason 缺失的空 content 是适配层/流异常特征，按生成失败降级。

**语义边界**：

- `finish_reason="stop"` + content 空 → `refusal`（保留，DeepSeek 形态，现有测试 `test_empty_content_normal_finish_treated_as_refusal` 不破坏）；
- `finish_reason=None` + content 空 → `empty` → `_try_extract`/`_fallback_extract` 返回 None 触发降级（修复适配层空响应误判）；
- `refusal` 字段 / `content_filter` / `tool_calls` / 截断 → 各自分支不变。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | `_classify_result` content 空分支区分 finish_reason（空 → `"empty"`）；`_try_extract` / `_fallback_extract` 对 `"empty"` 返回 None（触发降级） | `test_generate_structured.py` 新增 `test_empty_content_no_finish_treated_as_no_result` |
| `app/integration/llm/structured.py`（2026-08-16 补充） | **回喂循环**对 retry 分类 `"empty"` → 返回 None（LLM-004 遗漏处：回喂空响应不进回喂白打调用；修正变量名笔误 `retry_failure`→`failure` 避免正常路径 NameError） | `test_generate_structured.py` 新增 `test_reask_empty_response_returns_none` |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表加 LLM-004 条目） | — |

---

## 验证

- `tests/unit/test_generate_structured.py` **46 passed**（含空响应降级 + 回喂空响应降级 2 条；DeepSeek 拒答用例不破坏）
- 全量测试 **358 passed**（42.37s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **拒答必须基于显式信号，不能靠「内容空」推断**：`refusal` 字段 / `finish_reason=content_filter` 才是拒答信号；content 空可能是适配层空响应、reasoning 占用、模型空回等「生成失败」——按降级（放宽约束重试）处理，而非短路拒答。
- **分类要有「兜底类别」**：为「无任何明确信号的空结果」保留独立分类（empty → 业务无结果），避免所有异常形态都挤进最危险的短路类别（拒答）。
