# 自适应预留（Fenic 式）：高分位历史输出估算替代固定 max_tokens

> **状态**：✅ 已采纳（开关 `llm_adaptive_reserve` 默认关）
> **决策日期**：2026-08-06
> **涉及模块**：`app/integration/llm/reservation_limiter.py`（`reserve_adaptive` · `OutputTokenEstimator`）
> **关联文档**：[limiter.md](../../../docs/integration_doc/llm_doc/limiter.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## Context

- TPM 桶按 `prompt + max_tokens`（输出上限）预留——固定 `max_tokens` 预留导致预留期间**占桶（并发空耗）**：实际输出常远小于上限，但并发请求被多估的输出量卡住。
- 需用「历史实际输出的统计分布」预测下一次预留的输出量，替代固定上限。

## Decision

**`reserve_adaptive()` 用 `OutputTokenEstimator`（历史实际输出的高分位 × 安全系数）估算预留量，替代固定 `max_tokens`；结构性解耦——provider 仍收宽裕 `max_tokens`（不截断输出），只有限流器预留下降。**

1. **估算器**：`record(actual_output_tokens)` 在 settle 后喂实际输出；`estimate()` 返回「高分位 × 安全系数」——滚动样本（`deque(maxlen=256)`，Fenic 同款）、nearest-rank 分位、quantile clamp 到 `[0,1]`（配置异常防负索引）。
2. **分位数按模型**：普通模型 p95、推理模型 p99（推理输出有相关性突发尖峰），经 `ReservationLimiterManager` 按 model_key 配置。
3. **安全系数**：默认 1.15（可配 1.0~4.0），越高越保守、429 风险越低、吞吐越低。
4. **冷启动回退**：样本 < `min_samples`（30）返回 0 → 调用方回退静态 `max_tokens`。
5. **结构性解耦**：provider 收宽裕 `max_tokens` 不截断输出（模型能力不受限），仅限流器预留量下降。
6. **settle 回调**：`settle(actual)` 成功时喂样本；`settle(None)`/`cancel()` 不记录（无真实 usage 不污染分布）；按 `max_tokens` 分池（`_estimators: dict[int, OutputTokenEstimator]`）独立建模。

## Consequences

- **正面**：预留量反映真实输出分布（高分位），并发空耗降低；provider 输出能力不因限流预留截断；推理模型 p99 覆盖突发尖峰。
- **负面**：开关默认关（保守，未全面启用）；「实际消耗 > 预留」仍无法补扣（预留-结算模型结构性限制，宁多勿少保守取舍，已缓解未消除）；样本驱动需冷启动回退静态上限。
