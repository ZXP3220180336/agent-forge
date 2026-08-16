# 熔断窗口语义与请求级记账：RETRYABLE 计入 / 429 不计入 / fallback 隔离

> **状态**：✅ 已采纳
> **决策日期**：2026-08-01（初建）
> **涉及模块**：`app/integration/llm/retry.py`（`CircuitBreaker` / `RetryHandler` / `classify_error`）
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## Context

- 熔断器存在的意义是**识别并规避下游故障**——需要明确的「什么算故障」判定语义。
- 若把 RETRYABLE（超时/5xx）排除在错误率之外，窗口内只统计成功请求，错误率恒为 0——即便下游 5xx 成片，熔断器也不会打开，流量持续打到宕机的服务。
- fallback 是**备用链路**，纯兜底——不应污染主链路健康评估。
- 一次 `execute()` 含多次重试，若按调用记会造成错误率放大、熔断误判。

## Decision

1. **RETRYABLE（超时/5xx）计入滑动窗口错误率分子**——超时/5xx 是「下游故障」的直接证据；熔断器规避的就是这类故障。**429 不计入窗口**：429 是「客户端触发自身限额」，不是下游故障证据，只退避（`classify_error` 语义：RETRYABLE = 计入窗口失败 + 退避重试；RATE_LIMITED = 不计入窗口 + 退避重试）。
2. **fallback 成败不触碰窗口（fallback 隔离契约）**：成功不向窗口追加成功记录（否则稀释主链路错误率，主链路持续故障也永不熔断）；失败不追加失败记录、不改写冷却计时（备用链路的故障不是主链路故障的证据）。
3. **请求级记账**：一次 `execute()` 只向窗口追加一条记录，单请求的多次重试不放大错误率（[LLM-020](../../../issues/integration/llm/2026-08-01-request-level-accounting.md)）。
4. **参数关系**（各参数控制故障生命周期不同阶段）：
   - `max_retries` 与熔断判定**解耦**（记录粒度是请求级，互不影响）；
   - `window_seconds + error_threshold` 决定灵敏度——窗口越短/阈值越低越敏感、越易受偶发抖动影响；默认 `10s + 50%` 是工业常用起点；
   - `request_volume_threshold`（高流量按错误率）+ `all_failed_min`（低流量全部失败且达最小样本量即熔断）是**互补防误判机制**——一个防高流量误判、一个防低流量漏判；
   - `half_open_max_requests` 权衡恢复速度 vs 稳定性——太小（1）探针走运即恢复 → 频繁开关；太大（10）半开期白费 token → 恢复慢；一般 3~5。

## Consequences

- **正面**：熔断器识别真实下游故障（不被成功请求稀释）；fallback 不污染主链路健康；低流量不漏判（all_failed_min）、高流量不错判（request_volume_threshold）；单请求多次重试不虚增错误率。
- **负面**：429 不计入窗口 → 依赖指数退避 + `Retry-After` 尊重服务端建议（若服务端不返回 Retry-After，仅退避等待）；参数组合需按场景调优（默认/高可用敏感/深度容错三档参考组合在 retry.md「设计决策 Q3」）。
