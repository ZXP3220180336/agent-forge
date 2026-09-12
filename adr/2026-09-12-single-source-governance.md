# 单一文档维护体系与治理目标

日期：2026-09-12。决策状态：本次文档整合已采用。实现状态：文档迁移已完成；产品代码行为未在本任务修改或验证。

## Context

并存的治理草案与原文快照使规则来源变成第二套待维护文件。日后新增说明、问题和决策需要稳定归属，而不是不断复制入口和来源包。

## Decision

1. 根目录仅 AGENTS.md 为共享规则入口，CLAUDE.md 单向委托。原入口产品、工作流、测试和文档原则与治理规则已合并；不再保留原入口正文副本。
2. 通用 G0/E1–E9/G2/G3 在工程规范，项目事实归产品/架构/部署，详细接口和参数归既有模块文档。根入口用 Trigger 明确读取对应 Skill；Skill 负责执行，不复制规范。
3. 三份说明文档写作规范归 docs/engineering/documentation，共用规则只在其 README，记录类规则只在 records.md。
4. docs/ALIGNMENT.md、todo.md、lessons.md 保留原路径；产品/架构/部署归 docs/project。模块说明、ADR、Issue 保持正式位置，用当前文件互链追溯，不附原文快照。
5. 原入口的执行要求归入 [项目工作流](../docs/engineering/project-workflow.md)，图示要求归入 [文档规范](../docs/engineering/documentation/README.md)，入口和 Skill 只路由。用户于 2026-09-12 明确要求恢复原要求：撤回本次整合中对调研、写前方案确认、新增代码单元测试、全量测试及结构图格式的放宽。单一维护目录与既定 G0/G1/G2/G3、Trigger → Gate 保持。验证不复制历史 passed 数。

## 保留的局部契约

Reflection 自查早退以 [REASON-020](../issues/domain/reasoning/2026-09-11-reflection-guard-checkpoint.md)及其正式模块文档为准；不按通用模板暗改取消/成功语义。Planner 成功判据以 [REASON-019](../issues/domain/reasoning/2026-09-11-planner-step-success-extra-dimension.md)为准。两者不是本次重新设计的行为。

## G0-6 的后续代码符合性工作

历史上 [REASON-014](../issues/domain/reasoning/2026-09-09-internal-timeout-misclassified-as-deadline.md)与 [LLM-046](../issues/integration/llm/2026-09-09-continuation-finish-guard.md)记录了 EOF 后结算/日志异常上抛，以及日志 TimeoutError 导致 ReAct UNKNOWN 的路径。

后续 [ADR-003](2026-09-12-sdk-call-guard-response-commit.md) 明确区分必要结算与非关键观测，[LLM-049](../issues/integration/llm/2026-09-12-llm-observation-overrides-terminal.md) 已将 LLM 调用日志入口迁移为有界 best effort，同时保持结算错误与硬取消可见。该结论只覆盖 `fill_llm_event_fields` 的调用链；其他日志、指标和审计入口仍须按 G0-6 逐入口核验，不能从这一局部实现推导全仓已完成。

## Consequences

每项文档只有一份正式正文。旧已采纳条款遇到具名后继修订时按条款标注替代，不让旧历史影响当前路由。迁移不提供新业务功能、外部发布或代码改造授权。

本次路径对照及文档级验证见 [迁移说明](../docs/migration.md)。现状和验证状态由 [ALIGNMENT](../docs/ALIGNMENT.md)管理；本轮已接入完整仓库并核验文档映射，业务符合性未纳入迁移验收。外部编码规范来源缺口见[引用核对](../issues/documentation/2026-09-12-reference-gaps.md)。
