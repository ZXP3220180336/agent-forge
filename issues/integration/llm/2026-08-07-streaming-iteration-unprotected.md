# 流式迭代异常无保护：不重试不熔断不记录日志

> **状态**：✅ 已修复（2026-08-07）
> **优先级**：P1（中，熔断观察盲区）
> **来源**：2026-08-07 工业级改造 · 2026-08-16 从 retry.md 提取归档
> **涉及模块**：`app/integration/llm/llm_service.py`（流式迭代）· `app/integration/llm/streaming_rectifier.py`
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md)

---

## 问题描述

### 现象

`llm_service.py` 的 `async for chunk` 不在任何 try/except 内 → 流中断/解析失败时异常泄漏到调用方，**不重试、不熔断、不记录日志**。

### 影响

流式迭代中断无保护：调用方拿到裸异常、熔断器感知不到「create 正常但流频繁中断」的下游故障（熔断观察盲区）。

### 根因

`retry.execute` 只保护 `client.chat.completions.create()`（创建响应对象），真正的 `async for chunk in response:` 迭代在重试范围外。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| mid-stream 不重试 | OpenAI SDK `max_retries` 只覆盖初始 HTTP 请求，不重试 mid-stream——"用户已消费部分输出，mid-stream 重试语义不清晰" |
| 熔断观察盲区 | create 成功后流频繁中断是下游故障证据，熔断器应感知（最终放弃时喂 record_failure） |

---

## 修复方案（含决策取舍）

**决策**：流式迭代包进 try/except——失败时记录日志 + 产出错误事件（不重试，符合流式语义）；**最终放弃（不整流）且异常为 RETRYABLE 时喂 `cb.record_failure()`**，让熔断器感知「create 正常但流频繁中断」。

**修复要点**：

1. 迭代异常捕获 + 日志 + 错误事件；
2. 熔断观察盲区：放弃时 RETRYABLE 异常喂 `record_failure()`（整流重试中、NON_RETRYABLE/RATE_LIMITED/用户取消不计入）；
3. 该问题后续由 `StreamingRectifier`（整流策略）承载，失败信号透传（`StreamResult.error`，LLM-001）等演进见对应问题文档。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/llm_service.py` | 流式迭代 try/except + 错误事件 + 熔断 feeding | `test_stream_rectify.py` 迭代异常/熔断 feeding 用例 |
| `app/integration/llm/streaming_rectifier.py` | 整流策略独立承载迭代保护 | `test_streaming_rectifier.py` |

---

## 验证

- 流中断捕获记日志 + 错误事件；放弃时 RETRYABLE 喂熔断器
- 全量测试通过（2026-08-07 修复时验证）

---

## 教训沉淀

- **`retry.execute` 只保护 create 阶段**：流式迭代在重试范围外，必须单独保护（捕获 + 日志 + 事件），且熔断观察盲区要补（放弃时喂 record_failure）。
- **mid-stream 不重试**（OpenAI 共识）：已消费部分输出后重试语义不清晰，整流（WholeRestart）只在首 token 前进行。
