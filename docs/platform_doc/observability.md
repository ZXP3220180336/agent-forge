# 可观测性设计文档

> **更新日期**：2026-08-16
> **文档定位**：Agent 系统可观测性——日志、链路追踪、指标、告警的总纲与导航；
> 日志已实施，追踪 / 指标 / 告警待规划
> **实现状态**：🔶 日志已实施；链路追踪 / 指标 / 告警 ⬜ 待规划

---

## 📋 目录

- [定位与目标](#定位与目标)
- [日志](#日志)
- [链路追踪](#链路追踪)
- [指标](#指标)
- [告警](#告警)
- [相关文档](#相关文档)

---

## 定位与目标

Agent 系统可观测性的**总纲 + 导航**——多步 Agent 循环（推理 → 工具调用 → 观察）需要
链路级可诊断：每步延迟、token 消耗、失败点。工业级要求：**trace 关联一次完整 Agent
循环，指标量化调用面，告警及时暴露异常**。

**目标**：

1. **链路可追踪**：一次 Agent 循环的多步 LLM 调用 / 工具调用可关联定位
2. **调用面可量化**：token 用量、延迟、成功率、成本有指标与趋势
3. **异常可告警**：429 风暴、熔断开启、token 超限等异常及时告警

## 日志

> ✅ 已实施。全局 JSON 结构化日志 + 业务事件：`llm_call`（模型 / tokens / duration /
> success，只记元数据不记内容），`fill_llm_event_fields` 统一填充。详见
> [logging.md](observability/logging.md)。

## 链路追踪

> ⬜ 待规划。占位：trace 关联 ID（跨 Agent 循环多步）、span 设计（LLM 调用 /
> 工具调用 / 整流重试）、与日志事件关联。可评估 OpenTelemetry / 外部 tracing。

## 指标

> ⬜ 待规划。占位：token 用量（按模型）、延迟分布、成功率、成本、限流等待时间、
> 整流重试次数。可评估 Prometheus 指标暴露。

## 告警

> ⬜ 待规划。占位：429 触发率、熔断开启、token 配额接近上限、成本异常增长、
> 限流等待过长的告警规则与阈值。

---

## 相关文档

- [logging.md](observability/logging.md)（全局日志框架，`llm_call` 事件）
- [llm.md](../integration_doc/llm_doc/llm.md)（LLM 调用面）
- [llm_service.md](../integration_doc/llm_doc/llm_service.md)（可靠性链，
  可观测性观察对象）
- [config.md](../config_doc/config.md)（阈值配置）
