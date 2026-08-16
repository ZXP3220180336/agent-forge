# 重试与熔断架构：CircuitBreaker + 指数退避 + 抖动 + fallback 降级

> **状态**：✅ 已采纳
> **决策日期**：2026-08-01（初建；配置注入 08-09/08-10 增强）
> **涉及模块**：`app/integration/llm/retry.py`（`RetryHandler` / `RetryHandlerManager` / `CircuitBreaker` / `classify_error`）· `app/integration/llm/llm_service.py`
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## Context

- 临时故障（超时/5xx）需**自动重试**，无需上层感知；连续故障需**熔断**，防止对已宕机的下游发无用请求。
- 多并发同时重试产生**羊群效应**——随机抖动是必需项（多 Agent 并发时尤其重要）。
- 主模型不可用时需**降级**保证服务不中断。
- Python 生态有 `tenacity` 等通用重试库可替代自研。

## Decision

**自研 `RetryHandler`，采用「CircuitBreaker 滑动窗口熔断 + 指数退避 + 随机抖动 + fallback 降级 + 错误分类白名单」组合。**

1. **指数退避 × 2 + 随机抖动**（上限 `max_delay` 默认 30s）：快失败、慢重试，给服务端恢复时间；抖动防羊群。`Retry-After` 叠加（2026-08-16 封顶）：429 时在合理区间 `0 < retry_after ≤ max_delay` 取 `max(delay, retry_after)`，超限忽略回退指数退避（对齐 OpenAI SDK 合理区间判断，防 `retry-after: 3600` 挂死）。
2. **CircuitBreaker 滑动窗口错误率 + 半开探针**：窗口内错误率 ≥ 阈值且请求量达标 → OPEN；半开状态放行探针验证下游恢复。快速失败而非空等，探针自动恢复。
3. **fallback 降级链**：主模型不可用降级到**同服务商**便宜模型（复用主模型端点/密钥；跨服务商需独立配置，[LLM-012](../../../issues/integration/llm/2026-08-16-fallback-same-provider.md)）。
4. **错误分类白名单 `classify_error`**：RETRYABLE / RATE_LIMITED / NON_RETRYABLE 三态（未知默认不可重试，[LLM-021](../../../issues/integration/llm/2026-08-01-error-classification-whitelist.md)）。
5. **自研而非 `tenacity`**：本项目需要熔断 + fallback + 错误分类的**紧耦合编排**，tenacity 装饰器模式不适合该控制流；熔断器需**跨请求共享状态**（类级别，装饰器难表达——经 `RetryHandlerManager` 按 model_key 缓存共享实现，[LLM-023](../../../issues/integration/llm/2026-08-07-circuit-breaker-lifecycle.md)）；重试逻辑本身不到 100 行，自实现更透明、易调试。
6. **配置对象 + Manager 注入**（2026-08-09/10）：`RetryConfig` / `CircuitBreakerConfig` 纯配置 dataclass（默认值硬编码合理值），`RetryHandlerManager.register_config()` 注入——`Container.initialize()` 读 settings 组装注入，子模块零 settings 依赖，测试可隔离注入。

## Consequences

- **正面**：临时故障自动恢复；下游宕机时快速失败不空等、探针自动恢复；多 Agent 并发不羊群；主模型故障仍可用（fallback）；配置经 `.env` 重启生效无需改代码。
- **负面**：fallback 只支持同 provider（跨服务商降级需独立 endpoint/key 配置，当前不提供）；自研可靠性需测试覆盖维护；熔断窗口语义需精细调参（见 [ADR LLM-ADR-007](2026-08-01-circuit-breaker-window-semantics.md)）。
