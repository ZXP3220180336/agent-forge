# LLM 模块决策记录（ADR）

> **用途**：登记 Integration 层 LLM 模块（`app/integration/llm/`）的结构性/契约性设计决策，记录 Context → Decision → Consequences 完整前因后果，供追溯与复用。
> **更新日期**：2026-09-10
> **关联**：[LLM 层说明文档](../../../docs/integration_doc/llm_doc/llm.md) · [问题追踪](../../../issues/integration/llm/README.md)

## 决策索引

| ID | 决策 | 状态 | 涉及模块 | 决策日期 |
| --- | --- | --- | --- | --- |
| [LLM-ADR-001](2026-08-07-unified-structured-entry-degradation.md) | 统一结构化输出入口 + 三级降级策略 | ✅ 已采纳 | llm_service / structured | 2026-08-07 |
| [LLM-ADR-002](2026-08-15-pricing-prefix-match-fixed-table.md) | 定价查找：最长前缀匹配 + 模块内固定定价表 | ✅ 已采纳 | cost_tracker | 2026-08-15 |
| [LLM-ADR-003](2026-08-01-streaming-parse-pure-function.md) | 流式解析策略：纯函数无状态 + tool_call 延迟组装 + usage 独立提取 | ✅ 已采纳 | streaming | 2026-08-01 |
| [LLM-ADR-004](2026-08-01-client-pool-lazy-close-tracking.md) | 连接池管理：懒加载 + 主动关闭 + 热切换关闭追踪 | ✅ 已采纳 | client | 2026-08-01 |
| [LLM-ADR-005](2026-08-01-streaming-rectification-retry.md) | 流式整流重试策略：首 token 前中断自动恢复 | ✅ 已采纳 | streaming_rectifier / llm_service | 2026-08-01 |
| [LLM-ADR-006](2026-08-01-retry-circuit-breaker-architecture.md) | 重试与熔断架构：CircuitBreaker + 指数退避 + 抖动 + fallback | ✅ 已采纳 | retry / llm_service | 2026-08-01 |
| [LLM-ADR-007](2026-08-01-circuit-breaker-window-semantics.md) | 熔断窗口语义与请求级记账（RETRYABLE 计入 / fallback 隔离） | ✅ 已采纳 | retry | 2026-08-01 |
| [LLM-ADR-008](2026-08-01-rate-limit-token-bucket-waiting.md) | 客户端限流：Token Bucket 算法 + LLM 等待语义 | ✅ 已采纳 | reservation_limiter / llm_service | 2026-08-01 |
| [LLM-ADR-009](2026-08-02-reserve-settle-semantics.md) | reserve/settle 预留-结算形态（按实际 usage 退差） | ✅ 已采纳 | reservation_limiter / llm_service | 2026-08-02 |
| [LLM-ADR-010](2026-08-06-adaptive-reserve-output-estimator.md) | 自适应预留（Fenic 式）：高分位输出估算替代固定 max_tokens | ✅ 已采纳 | reservation_limiter | 2026-08-06 |
| [LLM-ADR-011](2026-08-04-llm-event-logging.md) | LLM 层日志：全局 JSON 结构化 + llm_call 业务事件 | ✅ 已采纳 | observability(logger) / llm | 2026-08-04 |
| [LLM-ADR-012](2026-08-24-token-counter-port.md) | TokenCounter 端口：tiktoken 隔离到集成层（依赖倒置 + 单一事实源） | 🔶 已替代 | token_counter / context_manager / llm_service | 2026-08-24 |
| [LLM-ADR-013](2026-09-01-openai-error-normalization.md) | openai 异常归一：LLMAPIError 入 AppError 树（generate 下游不可恢复统一决策） | ✅ 已采纳 | errors / llm_service / structured / shared | 2026-09-01 |
| [LLM-ADR-014](2026-09-03-connection-establishment-phase.md) | 连接建立期异常全景与守护机制（分级超时 connect/read/write/pool + 连接池 limits + 首包/空闲双阈值看门狗实施） | ✅ 已采纳 | client / errors / retry / reservation_limiter / streaming_rectifier / settings / container | 2026-09-03 |
| [LLM-ADR-015](2026-09-03-mid-stream-continuation.md) | 半流中断接续策略（text-only prefix continuation：已产出 content 带前缀续写，尽力而为退化放弃） | ✅ 已采纳 | streaming_rectifier / llm_service / settings / container / llm_gateway | 2026-09-03 |
| [LLM-ADR-016](2026-09-06-request-context-budget.md) | Provider 请求上下文准入：语义预算与最终请求预算分离 | ✅ 已采纳 | request_budget / llm_service / token_counter | 2026-09-06 |

登记、状态和维护规则见 [记录规范](../../../docs/engineering/documentation/records.md)。本表保留本模块的既有编号序列；历史状态为当时记录，不代表本轮重新验证。
