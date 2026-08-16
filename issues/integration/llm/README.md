# LLM 模块问题追踪

> **用途**：登记 Integration 层 LLM 模块（`app/integration/llm/` 及其跨模块关联方）审查/审核发现的问题，追踪从发现 → 分析 → 修复 → 验证的完整生命周期。
> **更新日期**：2026-08-16
> **关联**：[LLM 层说明文档](../../../docs/integration_doc/llm_doc/llm.md) · [领域端口契约](../../../docs/domain_doc/README.md)

## 状态图例

| 状态 | 含义 |
| --- | --- |
| 🔴 待修复 | 已登记未修复 |
| 🟡 修复中 | 修复实施中（代码/测试/文档） |
| ✅ 已修复 | 代码 + 测试 + 文档全部完成，已验证 |
| ⬜ 已放弃 | 评估后不修（附理由） |

## 问题索引

| ID | 标题 | 优先级 | 状态 | 涉及模块 | 登记日期 | 修复日期 |
| --- | --- | --- | --- | --- | --- | --- |
| [LLM-001](2026-08-16-stream-error-propagation.md) | 流式 create 阶段失败被 ReActAgent 静默吞掉 | P0 | ✅ 已修复 | streaming_rectifier / domain(StreamResult) / executor | 2026-08-16 | 2026-08-16 |
| [LLM-002](2026-08-16-generate-quota-settle-fallback.md) | 非流式 generate() 配额结算无兜底 | P0 | ✅ 已修复 | llm_service（非流式 generate） | 2026-08-16 | 2026-08-16 |
| [LLM-003](2026-08-16-hard-cancel-rpm-refund.md) | 流式硬取消对已发出请求退回 RPM 配额 | P1 | ✅ 已修复 | streaming_rectifier（迭代 finally） | 2026-08-16 | 2026-08-16 |
| [LLM-004](2026-08-16-empty-content-refusal-misjudge.md) | 空 content 一律归 refusal，适配层空响应误判拒答 | P1 | ✅ 已修复 | structured（_classify_result） | 2026-08-16 | 2026-08-16 |
| [LLM-005](2026-08-16-circuit-breaker-concurrency-tests.md) | retry 熔断器并发路径零测试覆盖 | P2 | ✅ 已修复 | retry（CircuitBreaker 并发） | 2026-08-16 | 2026-08-16 |
| [LLM-006](2026-08-16-rectify-entry-cancel-check.md) | 整流重试前不检查 cancel_event，取消后仍发真实请求 | P1 | ✅ 已修复 | streaming_rectifier（整流循环入口） | 2026-08-16 | 2026-08-16 |
| [LLM-007](2026-08-16-invalid-schema-crash.md) | 非法 schema 使 iter_errors 崩溃，防护不一致 | P1 | ✅ 已修复 | structured（_collect_schema_errors） | 2026-08-16 | 2026-08-16 |
| [LLM-008](2026-08-16-refusal-log-truncation.md) | 拒答文本完整落日志，违反安全基线 | P0 | ✅ 已修复 | structured（_raise_boundary 日志） | 2026-08-16 | 2026-08-16 |
| [LLM-009](2026-08-16-strict-additional-properties-true.md) | strict + additionalProperties:true 必然 400 | P1 | ✅ 已修复 | structured（_build_json_schema_request） | 2026-08-16 | 2026-08-16 |
| [LLM-010](2026-08-16-settle-cancel-concurrent-race.md) | settle/cancel 终态幂等并发竞态 | P1 | ✅ 已修复 | reservation_limiter（Reservation） | 2026-08-16 | 2026-08-16 |
| [LLM-011](2026-08-16-cancel-race-feeds-breaker.md) | 整流放弃分支 cancel 竞态错误喂熔断器 | P1 | ✅ 已修复 | streaming_rectifier（迭代放弃分支） | 2026-08-16 | 2026-08-16 |
| [LLM-012](2026-08-16-fallback-same-provider.md) | fallback 只支持同 provider（文档-实现偏差） | P1 | ✅ 已修复 | llm_service（_build_fallback_fn）+ 文档 | 2026-08-16 | 2026-08-16 |
| [LLM-013](2026-08-16-record-failure-return-contract.md) | record_failure bool 契约无人兑现 + retry.md 误导 | P2 | ✅ 已修复 | retry（record_failure）+ 文档 | 2026-08-16 | 2026-08-16 |
| [LLM-014](2026-08-16-single-entry-settle-test-false-positive.md) | 单条目 settle 测试假阳性 | P2 | ✅ 已修复 | test_reservation_limiter | 2026-08-16 | 2026-08-16 |
| [LLM-015](2026-08-08-no-schema-validation.md) | 解析后无 Schema 校验 | P0 | ✅ 已修复 | structured（_validate_schema） | 2026-08-08 | 2026-08-08 |
| [LLM-016](2026-08-08-finish-reason-refusal-unchecked.md) | 不检查 finish_reason/refusal | P1 | ✅ 已修复 | structured（_classify_result） | 2026-08-08 | 2026-08-08 |
| [LLM-017](2026-08-08-degrade-instead-of-error-reask.md) | 降级而非错误感知重试 | P1 | ✅ 已修复 | structured（_try_extract 回喂） | 2026-08-08 | 2026-08-08 |
| [LLM-018](2026-08-08-extra-fields-not-rejected.md) | 额外字段不拒绝 | P2 | ✅ 已修复 | structured（_enforce_no_extra_fields） | 2026-08-08 | 2026-08-08 |
| [LLM-019](2026-08-01-sliding-window-circuit-breaker.md) | 滑动窗口熔断改造（计数/时间/429） | P0 | ✅ 已修复 | retry（CircuitBreaker） | 2026-08-01 | 2026-08-01 |
| [LLM-020](2026-08-01-request-level-accounting.md) | 请求级记账（触发后仍重试/混合失败） | P1 | ✅ 已修复 | retry（RetryHandler） | 2026-08-01 | 2026-08-01 |
| [LLM-021](2026-08-01-error-classification-whitelist.md) | 错误分类白名单（未知默认不可重试） | P1 | ✅ 已修复 | retry（classify_error） | 2026-08-01 | 2026-08-01 |
| [LLM-022](2026-08-05-half-open-probe-semantics.md) | 半开探针语义演进（死锁/429/4xx/取消） | P0 | ✅ 已修复 | retry（_probe_attempt） | 2026-08-05 | 2026-08-05 |
| [LLM-023](2026-08-07-circuit-breaker-lifecycle.md) | 熔断器生命周期共享（每次 new 熔断失效） | P0 | ✅ 已修复 | retry（RetryHandlerManager） | 2026-08-07 | 2026-08-07 |
| [LLM-024](2026-08-07-streaming-iteration-unprotected.md) | 流式迭代异常无保护（熔断观察盲区） | P1 | ✅ 已修复 | llm_service / streaming_rectifier | 2026-08-07 | 2026-08-07 |
| [LLM-025](2026-08-09-fallback-isolation.md) | fallback 隔离（成败不进状态机） | P1 | ✅ 已修复 | retry（fallback 路径） | 2026-08-09 | 2026-08-09 |
| [LLM-026](2026-08-02-zero-refill-crash.md) | 配置为 0 时除零崩溃（限流禁用表达） | P0 | ✅ 已修复 | reservation_limiter | 2026-08-02 | 2026-08-02 |
| [LLM-027](2026-08-02-lock-hold-sleep.md) | TokenBucket 持锁 sleep（连带负 token） | P1 | ✅ 已修复 | reservation_limiter | 2026-08-02 | 2026-08-02 |
| [LLM-028](2026-08-02-tpm-prompt-only.md) | TPM 只算 prompt token（加输出余量） | P1 | ✅ 已修复 | llm_service / reservation_limiter | 2026-08-02 | 2026-08-02 |
| [LLM-029](2026-08-02-api-clarity-fixes.md) | 限流器 API 清晰性（返回值/async with） | P3 | ✅ 已修复 | reservation_limiter | 2026-08-02 | 2026-08-02 |
| [LLM-030](2026-08-07-stream-parser-robustness.md) | 流式/非流式解析健壮性 | P1 | ✅ 已修复 | streaming / streaming_rectifier | 2026-08-07 | 2026-08-07 |
| [LLM-031](2026-08-09-client-close-tracking.md) | 连接关闭追踪演进（静默忽略/task 泄漏） | P1 | ✅ 已修复 | client（ClientManager） | 2026-08-09 | 2026-08-16 |
| [LLM-032](2026-08-11-close-all-iteration-safety.md) | close_all 迭代安全（RuntimeError/异常中断） | P1 | ✅ 已修复 | client（close_all） | 2026-08-11 | 2026-08-11 |
| [LLM-033](2026-08-09-client-kwargs-whitelist.md) | 参数透传白名单（**extra 吞没） | P1 | ✅ 已修复 | client（get_client） | 2026-08-09 | 2026-08-09 |
| [LLM-034](2026-08-02-quota-gap-retry-degradation-not-limited.md) | 配额缺口：重试/降级不计入限流申请 | P0 | ✅ 已修复 | reservation_limiter / retry / llm_service | 2026-08-02 | 2026-08-02 |
| [LLM-035](2026-08-10-rectify-emitted-any-marker-reset.md) | emitted_any 累积语义缺失（误整流重复输出） | P1 | ✅ 已修复 | streaming_rectifier | 2026-08-10 | 2026-08-10 |
| [LLM-036](2026-08-16-generate-structured-model-key-param.md) | generate_structured 参数名契约（model_key） | P3 | ✅ 已修复 | llm_service / structured | 2026-08-16 | — |
| [LLM-037](2026-08-16-schema-validation-log-redaction.md) | 校验失败日志未脱敏（jsonschema e.message 嵌入完整实例值） | P0 | ✅ 已修复 | structured（_validate_schema / _collect_schema_error_summaries） | 2026-08-16 | 2026-08-16 |

## 新问题登记规范

1. **命名**：`<日期>-<短横线描述>.md`——日期为登记日（`YYYY-MM-DD`），描述为该问题的短 slug（对齐 CLAUDE.md 新规则「文件名使用 <日期-问题> 方式命名」）。
2. **编号（索引 ID）**：LLM-XXX 递增（001、002、…），仅用于索引表展示；**文件名不含编号**。
3. **模板**：复制既有问题文件的结构——元信息块（状态/优先级/来源/涉及模块）→ 问题描述（现象/影响/根因）→ 工业级参照 → 修复方案（含决策取舍）→ 实施记录（文件×改动×回归测试）→ 验证 → 教训沉淀。
4. **登记**：新建文件后同步更新上方索引表（ID / 标题 / 优先级 / 状态 / 涉及模块 / 登记日期 / 修复日期）。
5. **修复闭环**：修复完成后更新状态为 ✅ + 修复日期，并同步对应模块文档（LLM 层工作流 gate：改代码必改对应模块文档）。

## 维护原则

- **与 todo.md / lessons.md 分离**：本目录沉淀「问题从发现到验证的完整生命周期」（可回溯、可审计）；todo.md 是「将来要做什么」；lessons.md 是「纠正后的结论」。
- **一个文件一个问题**：跨模块问题以主因模块归位（如 LLM-001 归 llm 模块，涉及 domain/agent 在文件内说明）。
- **目录与 app/ 结构对齐**：`issues/<层名>/<模块名>/`（本目录 = `issues/integration/llm/` 对应 `app/integration/llm/`）；新层/模块的问题归入对应目录。
