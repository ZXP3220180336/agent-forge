# LLM 模块问题追踪

> **用途**：登记 Integration 层 LLM 模块（`app/integration/llm/` 及其跨模块关联方）审查/审核发现的问题，追踪从发现 → 分析 → 修复 → 验证的完整生命周期。
> **更新日期**：2026-08-16
> **关联**：[LLM 层说明文档](../../integration_doc/llm_doc/llm.md) · [领域端口契约](../../domain_doc/README.md)

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
| [LLM-001](llm-001-stream-error-propagation.md) | 流式 create 阶段失败被 ReActAgent 静默吞掉 | P0 | ✅ 已修复 | streaming_rectifier / domain(StreamResult) / executor | 2026-08-16 | 2026-08-16 |
| [LLM-002](llm-002-generate-quota-settle-fallback.md) | 非流式 generate() 配额结算无兜底 | P0 | ✅ 已修复 | llm_service（非流式 generate） | 2026-08-16 | 2026-08-16 |
| [LLM-003](llm-003-hard-cancel-rpm-refund.md) | 流式硬取消对已发出请求退回 RPM 配额 | P1 | ✅ 已修复 | streaming_rectifier（迭代 finally） | 2026-08-16 | 2026-08-16 |
| [LLM-004](llm-004-empty-content-refusal-misjudge.md) | 空 content 一律归 refusal，适配层空响应误判拒答 | P1 | ✅ 已修复 | structured（_classify_result） | 2026-08-16 | 2026-08-16 |
| [LLM-005](llm-005-circuit-breaker-concurrency-tests.md) | retry 熔断器并发路径零测试覆盖 | P2 | ✅ 已修复 | retry（CircuitBreaker 并发） | 2026-08-16 | 2026-08-16 |
| [LLM-006](llm-006-rectify-entry-cancel-check.md) | 整流重试前不检查 cancel_event，取消后仍发真实请求 | P1 | ✅ 已修复 | streaming_rectifier（整流循环入口） | 2026-08-16 | 2026-08-16 |
| [LLM-007](llm-007-invalid-schema-crash.md) | 非法 schema 使 iter_errors 崩溃，防护不一致 | P1 | ✅ 已修复 | structured（_collect_schema_errors） | 2026-08-16 | 2026-08-16 |
| [LLM-008](llm-008-refusal-log-truncation.md) | 拒答文本完整落日志，违反安全基线 | P0 | ✅ 已修复 | structured（_raise_boundary 日志） | 2026-08-16 | 2026-08-16 |
| [LLM-009](llm-009-strict-additional-properties-true.md) | strict + additionalProperties:true 必然 400 | P1 | ✅ 已修复 | structured（_build_json_schema_request） | 2026-08-16 | 2026-08-16 |
| [LLM-010](llm-010-settle-cancel-concurrent-race.md) | settle/cancel 终态幂等并发竞态 | P1 | ✅ 已修复 | reservation_limiter（Reservation） | 2026-08-16 | 2026-08-16 |

## 新问题登记规范

1. **编号**：`llm-XXX-<短横线描述>.md`，XXX 递增（001、002、…），描述为该问题的短 slug。
2. **模板**：复制既有问题文件的结构——元信息块（状态/优先级/来源/涉及模块）→ 问题描述（现象/影响/根因）→ 工业级参照 → 修复方案（含决策取舍）→ 实施记录（文件×改动×回归测试）→ 验证 → 教训沉淀。
3. **登记**：新建文件后同步更新上方索引表（ID / 标题 / 优先级 / 状态 / 涉及模块 / 登记日期 / 修复日期）。
4. **修复闭环**：修复完成后更新状态为 ✅ + 修复日期，并同步对应模块文档（LLM 层工作流 gate：改代码必改对应模块文档）。

## 维护原则

- **与 todo.md / lessons.md 分离**：本目录沉淀「问题从发现到验证的完整生命周期」（可回溯、可审计）；todo.md 是「将来要做什么」；lessons.md 是「纠正后的结论」。
- **一个文件一个问题**：跨模块问题以主因模块归位（如 LLM-001 归 llm 模块，涉及 domain/agent 在文件内说明）。
