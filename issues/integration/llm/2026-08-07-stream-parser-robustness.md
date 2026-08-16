# 流式/非流式解析健壮性：finish_reason 丢失、usage 丢弃、tool_deltas 残留、空 choices 崩溃

> **状态**：✅ 已修复（2026-08-07 / 2026-08-09）
> **优先级**：P1（中）
> **来源**：2026-08-07 / 08-09 审核修复 · 2026-08-16 从 streaming.md 提取归档
> **涉及模块**：`app/integration/llm/streaming.py`（`StreamParser`）· `app/integration/llm/streaming_rectifier.py`
> **关联文档**：[streaming.md](../../../docs/integration_doc/llm_doc/streaming.md)

---

## 问题描述

四个解析健壮性缺陷：

| # | 问题 | 后果 |
| --- | --- | --- |
| 1 | **delta=None 丢 finish_reason（2026-08-07）** | `parse_chunk` 原守卫 `not chunk.choices[0].delta` 在 delta=None 的 finish chunk 上提前 return，丢失 finish_reason |
| 2 | **usage 与空 delta 共存时静默丢弃（2026-08-07）** | usage 提取依赖 choices 为空——代理层违规在带 delta 的 chunk 上附 usage 时被丢弃 |
| 3 | **整流 continue 前未清空 tool_deltas（2026-08-07）** | 整流重试 `continue` 前未显式清空，防未来重构携带脏数据 |
| 4 | **非流式空 choices 抛 IndexError（2026-08-09）** | `parse_non_stream` 直接 `response.choices[0]`，适配层/异常响应空 choices 抛裸 IndexError |

### 影响

finish_reason / usage 元数据丢失（下游判定出错）；tool_call 增量污染整流重试；空 choices 崩溃（调用方拿到不可读索引异常而非「业务无结果」）。

### 根因

解析器对异常 chunk/响应形态缺乏防御——finish_reason/usage 提取依赖错误前置条件、整流未清理累积、空 choices 无防护。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| finish_reason 独立提取 | finish chunk 的 delta 可能为 None，finish_reason 在 choices[0] 上——不能因 delta 为空丢失 |
| usage 独立提取 | usage 只信最终 chunk，不期待每个 delta 都有；代理层违规共存时不静默丢弃 |
| 空 choices 防护 | 适配层/异常响应可能返回空 choices——返回空结果（业务无结果）而非裸 IndexError |
| 整流幂等 | 整流重试重新迭代，每次迭代的增量独立——防御性清空 tool_deltas |

---

## 修复方案（含决策取舍）

**决策**：`parse_chunk` 重构——finish_reason / usage 独立于 delta/choices 提取；整流 continue 前清空 tool_deltas；`parse_non_stream` 空 choices 防护。

**修复要点**：

1. **finish_reason 独立提取**：不依赖 delta 是否为空（delta=None 的 finish chunk 也拿到 finish_reason）；
2. **usage 独立提取**：不依赖 choices 为空（带 delta 的 chunk 附 usage 时不丢弃）；
3. **整流幂等**：`StreamingRectifier` 整流 `continue` 前防御性清空 `tool_deltas`；
4. **空 choices 防护**：`parse_non_stream` 空 choices 返回空结果（content=""、finish_reason=None、usage 透传）——调用方按「业务无结果」处理；
5. **refusal 透传**：保留 None 与空串区分（`or ""` 会抹掉拒答形态）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/streaming.py` | `parse_chunk` finish_reason/usage 独立提取；`parse_non_stream` 空 choices 防护 | `test_streaming.py` finish chunk/usage 共存/空 choices 用例 |
| `app/integration/llm/streaming_rectifier.py` | 整流 continue 前清空 tool_deltas | `test_streaming_rectifier.py` 整流幂等用例 |

---

## 验证

- finish chunk 拿到 finish_reason；usage 共存不丢弃；空 choices 不崩溃（返回业务无结果）
- 全量测试通过（2026-08-07 + 08-09 修复时验证）

---

## 教训沉淀

- **元数据提取独立于前置条件**：finish_reason / usage 不依赖 delta/choices 形态——异常 chunk 形态下元数据不丢失。
- **整流重试必须幂等**：每次迭代增量独立，continue 前清空累积（tool_deltas），防脏数据污染。
- **空响应按「业务无结果」处理**：适配层空 choices 返回空结果而非裸 IndexError。
