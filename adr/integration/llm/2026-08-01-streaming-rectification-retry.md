# 流式整流重试策略：首 token 前中断自动恢复

> **状态**：✅ 已采纳
> **决策日期**：2026-08-01（实施）· 2026-08-10（拆独立策略类）
> **涉及模块**：`app/integration/llm/streaming_rectifier.py`（`StreamingRectifier`）· `app/integration/llm/llm_service.py`（`async_generate`）
> **关联文档**：[streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [limiter.md](../../../docs/integration_doc/llm_doc/limiter.md)

---

## Context

- `retry.execute()` 只保护 `client.chat.completions.create()`（创建响应对象），真正的 `async for chunk in response:` 迭代在重试范围外——流中途断掉（读超时 / 连接重置 / 解析失败）时旧实现只捕获报错（记日志 + 错误事件），**不重试**。
- **首 token 前中断 vs 已产出后中断语义不同**：首 token 前用户没看到任何输出，可安全重试不产生重复；已产出后重试产生重复内容 + token 双倍计费 + tool_calls 残缺。
- create 阶段（HTTP 请求级重试）与已开始流式后的中断属**不同故障阶段**，需独立配置调优。
- 整流循环与 Facade 编排（create_fn / 事件日志 / 结算）职责纠缠，需独立策略类保持正交。

## Decision

**流式中断按「首 token 是否已产出」分流：首 token 前中断 → 整流重试（重新 create + 重新迭代）；已产出后中断 → 不整流。**

1. **整流条件**（`_should_rectify`，全部满足才整流）：
   - 首 token 前（`emitted_any=False`）——`reasoning_token` / `message_token` / `tool_call_deltas` 任一非空即置位；`finish_reason` / `usage` **不算**"首 token"（纯 usage/finish 死流仍可整流）；
   - 未超整流上限（`attempt < stream_max_retries`）；
   - 异常可恢复（`classify_error` ∈ `RETRYABLE` / `RATE_LIMITED`；NON_RETRYABLE 如 4xx/校验错误/截断/未知不整流）；
   - 用户未取消（`cancel_event` 未置位）。
2. **create 阶段异常绝不整流**：`retry.execute` 已决定重试/熔断/fallback——400 / `CircuitBreakerOpenError` / fallback 失败走既有错误路径。
3. **独立配置 `llm_stream_max_retries`**（默认 1），不复用 `llm_max_retries`——create 重试与整流重试属不同故障阶段；设为 `0` 即禁用整流。
4. **独立策略类 `StreamingRectifier`**（无状态静态类）+ `RectifierContext`（result/active/event_fields 共享状态）——整流循环 / `emitted_any` / 熔断 feeding / 结算闭环 / 事件日志与 Facade 编排正交（2026-08-10 拆分）；`async_generate` 只做编排（构造 `create_fn` + 调 `rectified_stream`）。
5. **结算闭环**（reservation）：成功读完 `settle(actual)` 退 TPM 差；迭代中断/用户取消 `settle(actual)`（请求已发出，无论整流与否）；硬取消（CancelledError）`finally` 兜底 `settle(None)` 保留配额 + 标记终态（[LLM-003](../../../issues/integration/llm/2026-08-16-hard-cancel-rpm-refund.md)）。每次整流 attempt 由 `create_fn` 重新 reserve（新请求语义，[LLM-034](../../../issues/integration/llm/2026-08-02-quota-gap-retry-degradation-not-limited.md)）。
6. **熔断 feeding**：流式迭代「放弃时」（不整流）且异常 RETRYABLE → `circuit_breaker.record_failure()`——让熔断器感知「create 正常但流频繁中途断开」的下游故障；整流成功不喂、NON_RETRYABLE / RATE_LIMITED / cancel 不喂（[LLM-024](../../../issues/integration/llm/2026-08-07-streaming-iteration-unprotected.md)）。

## Consequences

- **正面**：首 token 前中断自动恢复（用户无感，整流不产生重复内容）；已产出 token 后中断不整流（避免重复输出/双倍计费/tool_calls 残缺）；结算闭环无配额泄漏；熔断器感知流级故障（create 正常但流频繁中断）。
- **负面**：整流重试增加一次完整 create + 迭代（token 消耗，但仅在首 token 前中断时发生）；整流语义需要多道守卫——`emitted_any` 累积语义（[LLM-035](../../../issues/integration/llm/2026-08-10-rectify-emitted-any-marker-reset.md)）、取消后不发新副作用（[LLM-006](../../../issues/integration/llm/2026-08-16-rectify-entry-cancel-check.md)）、放弃分支取消守卫（[LLM-011](../../../issues/integration/llm/2026-08-16-cancel-race-feeds-breaker.md)）。
