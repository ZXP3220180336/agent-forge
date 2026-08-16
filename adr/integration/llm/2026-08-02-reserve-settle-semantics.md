# reserve/settle 预留-结算形态：按实际 usage 退还未用配额

> **状态**：✅ 已采纳
> **决策日期**：2026-08-02（reserve/settle 引入）· 2026-08-10（acquire 形态移除）
> **涉及模块**：`app/integration/llm/reservation_limiter.py`（`ReservationLimiter` / `Reservation`）· `app/integration/llm/llm_service.py`
> **关联文档**：[limiter.md](../../../docs/integration_doc/llm_doc/limiter.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## Context

- TPM 桶按 `prompt + max_tokens`（输出上限保守估算）预留，但实际输出往往远小于 `max_tokens`——长期偏保守低估可用量。
- acquire 形态一次性扣减、**无结算能力**（不退款）；调用方无法把未用完的配额还给桶。
- 需要按实际 usage 结清：请求完成后把未用的 TPM 配额退还给桶（`settle(actual)` 退差 / `cancel()` 全额退）。

## Decision

**生产唯一形态为 `reserve/settle`（`ReservationLimiter` + `Reservation`），acquire 形态从生产移除（2026-08-10，代码保留作学习参考）。**

1. **`reserve(estimated)` → `res.settle(actual)`**：预留配额（等待型，不拒绝），请求完成后按实际 usage 结算——`settle(actual)` 退还未用 TPM 配额差；`cancel()` 全额退还。
2. **终态幂等**：`Reservation.settle`/`cancel` 任一调用后再次调用为 no-op（防重复结算）；`settle(None)` 保留全部预留但标记终态（请求已发出，RPM 真实消耗不退回——[LLM-003](../../../issues/integration/llm/2026-08-16-hard-cancel-rpm-refund.md)）。
3. **结算退差的意义**：纠正「预估始终偏保守」的系统性低估——长期看桶反映真实可用量。
4. **工业级参照**：Go `x/time/rate` Reservation、LiteLLM / Fenic 预留-结算协议。
5. **acquire 形态保留为学习参考**：`RateLimiter`（双桶 + acquire 一次性扣减）及其 Manager 未接入调用链，接口与 TokenBucket 对齐，供对比与按需选用。

## Consequences

- **正面**：TPM 桶反映真实可用量（结算退差纠正系统性低估）；请求级记账准确（每轮重试重新 reserve = 新请求语义，[LLM-034](../../../issues/integration/llm/2026-08-02-quota-gap-retry-degradation-not-limited.md)）；终态幂等防重复结算。
- **负面**：`settle`/`cancel` 终态互斥需 `asyncio.Lock` 防护（并发重复退款，[LLM-010](../../../issues/integration/llm/2026-08-16-settle-cancel-concurrent-race.md)）；预留-结算模型「实际消耗 > 预留」仍无法补扣（结构性限制，宁多勿少保守取舍）。
