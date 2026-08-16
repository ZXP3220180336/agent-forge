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
4. **参数关系**（各参数控制故障生命周期中**不同阶段**）：
   ```text
   时间轴：  首次失败 → 重试 → 重试 → ... → 熔断开启 → 等待 recovery → 半开探针 → 关闭/继续熔断
              ├── max_retries 控制 ──┤
                                             ├ window_seconds + error_threshold 控制 ┤
                                                                     ├ half_open_max_requests ┤
   ```
   - `max_retries` — 单次请求的"挣扎"次数。控制一个请求在放弃前尝试几次
   - `window_seconds` — 熔断评估的时间范围。窗口内累计请求数与失败数，过期记录剔除
   - `error_threshold` — 窗口内错误率阈值。总请求达标时，错误率 ≥ 阈值 → 熔断
   - `request_volume_threshold` — 最小请求量门槛。窗口内请求数不足时不做错误率评估（防低流量误判）
   - `all_failed_min` — 低流量纯失败保护。窗口内**全部失败**且失败数达此值 → 熔断（即使请求量不足门槛）
   - `half_open_max_requests` — 恢复时的"验证"数量。控制半开状态下放行几个探针验证下游是否恢复

   **关系 1：`max_retries` 与熔断判定解耦**——记录粒度是**请求级**：一次 `execute()` 只向窗口追加一条记录，单请求的多次重试不放大错误率，所以 `max_retries` 与熔断判定**互不影响**。

   **关系 2：`window_seconds + error_threshold` 决定熔断灵敏度**——窗口越短、错误率阈值越低越敏感，也越易受偶发抖动影响；窗口越长统计越平滑、但对持续故障反应越慢；默认 `10s + 50%` 是工业常用起点。

   **关系 3：`request_volume_threshold` 与 `all_failed_min` 是防误判的互补机制**——高流量靠 `request_volume_threshold` + `error_threshold` 按错误率判断；低流量下 `request_volume_threshold` 永远达不到 → 靠 `all_failed_min` 全部失败且达最小样本量即熔断；一个防高流量误判、一个防低流量漏判。

   **关系 4：`half_open_max_requests` 决定恢复速度 vs 稳定性权衡**——太小（1）探针走运即恢复、恢复后被打爆 → 频繁开关；太大（10）半开期白费 token → 恢复慢；一般 3~5。

   **典型组合策略**：

   | 场景 | max_retries | window | error_threshold | volume | all_failed | half_open | 理由 |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | 默认保守 | 2 | 10 | 0.5 | 20 | 3 | 3 | 高流量按错误率 50% 熔断，低流量全部失败 3 次即熔断 |
   | 高可用/敏感 | 1 | 5 | 0.3 | 10 | 3 | 2 | 窗口短、阈值低 → 快速熔断 |
   | 深度容错 | 3 | 20 | 0.7 | 30 | 5 | 5 | 窗口长、阈值高 → 尽可能多试，大面积故障才熔断 |
   | 不熔断（纯重试） | 2 | 10 | 0.99 | 1000 | 999 | 3 | 阈值开到不可能触发，等于禁用熔断 |

## Consequences

- **正面**：熔断器识别真实下游故障（不被成功请求稀释）；fallback 不污染主链路健康；低流量不漏判（all_failed_min）、高流量不错判（request_volume_threshold）；单请求多次重试不虚增错误率。
- **负面**：429 不计入窗口 → 依赖指数退避 + `Retry-After` 尊重服务端建议（若服务端不返回 Retry-After，仅退避等待）；参数组合需按场景调优（默认/高可用敏感/深度容错/不熔断四档参考组合见决策 4 末表）。
