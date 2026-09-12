# 已完成工作的交接记录

整理日期：2026-09-12。本文件只保留原 `docs/todo.md` 中尚无独立 Issue/ADR 完整承载的有用交接结论，以及指向现行记录的索引。它不是规则正文，也不是旧 todo 全文存档。

原记录中的测试通过、提交和完成状态仅代表当时记录；治理迁移收尾未重跑这些历史业务测试。尚未关闭的事项只维护在[项目待办](../todo.md)。

## 2026-09-12：SDK 调用 Guard、响应接管与提交边界

[ADR-003](../../adr/2026-09-12-sdk-call-guard-response-commit.md) 已落地：调用前准入阻止下一
副作用；SDK 返回后先接管响应、usage 与 reservation，再按 Guard 类型决定提交。create
调度后的普通异常改为 `settle(None)`，LLM 调用日志改为有界 best effort；Reflection 的
strict deadline 保留稿件与 critique 后进入超时终态，Planner 的 STOP 不再启动 ReAct 或
付费汇总，上下文超限不再落入未缩减的普通 fallback。ReAct 经复核无需修改。

实施记录见 [LLM-048](../../issues/integration/llm/2026-09-12-create-started-ordinary-error-settlement.md)、
[LLM-049](../../issues/integration/llm/2026-09-12-llm-observation-overrides-terminal.md)、
[REASON-021](../../issues/domain/reasoning/2026-09-12-planner-stop-starts-fallback.md) 与
[REASON-022](../../issues/domain/reasoning/2026-09-12-structured-guard-terminal-loss.md)。专项回归
216 项通过，全量测试 968 项通过；`scripts.verify_alignment` 与 `git diff --check` 通过。
唯一告警为 Starlette 测试兼容层的既有 `httpx` 弃用提示。

## 2026-09-12：治理包接入与迁移收尾

治理包已合入基准 `c351a81` 的工作区，未提交。此前创建迁移分支受 Git 权限限制，本轮未重试分支操作。根入口单一化、正式目录迁移、源码引用恢复、索引去重及原工作流要求核对已完成；详细验收与范围见[迁移说明](../migration.md)，外部规范来源见[引用核对](../../issues/documentation/2026-09-12-reference-gaps.md)。

用户明确本轮只闭环治理搭建；代码符合性候选不作为迁移未完成项，仍统一保留在[当前计划](../todo.md)。未运行产品测试，历史 passed 数不作为本次证据。

## 独特交接结论

### 2026-09-10 至 09-11：批次收尾与测试基础设施

原“三策略重试上限闭环”最后一份批次回归记录为 **955 passed**，并记载 `verify_alignment`、`git diff --check` 通过，存在一个 StarletteDeprecationWarning。这是历史批次结果；不能把该数字作为当前仓库或 09-11 最后修改后的完整验证证明。

同批还记录了不宜另立产品规则的维护改动：四处测试无界等待改成有界失败；`agent_params` 收敛为 `tests/conftest.py` 共用 fixture；配置验证器数量、请求字段 None 注释、示例与实际入口语义同步。后续 Guard 复审把一处依赖“首轮须在预算内完成”的测试预算从 0.2 秒放宽至 1 秒，并将 `.gitignore` 的 `*-learn.md` 收窄到 `docs/*-learn.md`。这些是当时的具体修正，不是所有测试统一使用一秒或全部仓库采用该忽略规则的要求。

原 todo 的旧 Slice 2 未勾选、第一阶段“待提交”和 Planner“待实现”均已被更晚的完成条目覆盖。它们不再进入当前待办；修复依据见下方索引。原 Slice 3/4 及独立边界仍见[项目待办](../todo.md)，不在此再复制一份状态。

### 2026-09-01：LLM 错误文档的单一维护位置

错误分类、归一和下游决策说明集中到 [LLM 错误组件文档](../integration_doc/llm_doc/error.md)，retry、Facade、structured 与 streaming 文档以链接引用。原 todo 记载的是文档收敛和 ALIGNMENT 接线过程；错误类型与实现归属的现行决策见[异常归一 ADR](../../adr/integration/llm/2026-09-01-openai-error-normalization.md)及[REASON-010 遗留闭环](../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)。不恢复已撤回的 `shared/error_category.py` 或被删除的旧 ADR。

### 2026-08-30：Facade 导出面与装配根

LLM 包入口曾重导出内部子组件，与“LLMService 为消费方 Facade”不一致；原 todo 记载包入口收窄、包内引用改相对深路径，装配根为配置注册保留内部组件导入。这里保留这一未单独立档的迁移背景；现行接口以 [LLM 模块](../integration_doc/llm_doc/llm.md)和[集成规则](../engineering/integration-rules.md)为准，不将“组合根例外”扩大到业务消费方。

### 2026-08-15：文档镜像迁移与验证基础设施

文档随 `app/` 的分层迁移到 domain/application/integration/infrastructure/shared/platform，建立 [ALIGNMENT](../ALIGNMENT.md) 与 `scripts/verify_alignment.py` 的代码—文档—测试映射。原 todo 记录首批全量 224 passed，补齐测试后 325 passed；这里只保留两个迁移节点，不复制每份文件的操作清单。

当时构建测试替身发现：SQLAlchemy 聚合查询的 `column_descriptions.entity` 也可能是映射类，不能据此区分实体行与聚合；`expr` 与实际查询形状更有判别性。`db_session_factory` 返回值须匹配业务的异步上下文管理协议；SessionManager 的构造参数名不等于实例存储属性名。重复查询前需控制 fake Redis 缓存，否则测试可能未到达目标 DB 分支。配置测试显式关闭 `.env` 读取、工具测试清空真实 API key，以避免依赖本机秘密和真实网络。这些属于已遇到的测试误判机制，使用时仍需核对当前实现。

旧空目录删除曾受沙箱限制；git 不追踪空目录，未影响当时迁移。没有证据证明这些目录今天仍存在，因此不挂为新的清理任务。文档移动后的相对链接、包入口文件名和导入路径均在维护时按实际目录核验；不将早期 `(unknown location)` 导入排查经验解释为唯一根因。

## 已有 Issue / ADR 的完成索引

下表只导航，不重复展开已经有正式归属的规则和修复过程。

| 批次 / 主题 | 现行记录 |
| --- | --- |
| 最新 Guard 复审：计划形状、步骤判据、自查检查点 | [REASON-018](../../issues/domain/reasoning/2026-09-11-planner-plan-contract-shape.md) · [REASON-019](../../issues/domain/reasoning/2026-09-11-planner-step-success-extra-dimension.md) · [REASON-020](../../issues/domain/reasoning/2026-09-11-reflection-guard-checkpoint.md) |
| 跨策略 Guard 与协议预算 | [REASON-016](../../issues/domain/reasoning/2026-09-10-cross-strategy-guard-priority.md) · [REASON-017](../../issues/domain/reasoning/2026-09-10-tool-protocol-retry-limit.md) |
| 当前轮成果、清理窗口、超时来源、续接拒绝 | [REASON-012](../../issues/domain/reasoning/2026-09-09-deadline-current-round-progress.md) · [REASON-013](../../issues/domain/reasoning/2026-09-09-deadline-cleanup-grace.md) · [REASON-014](../../issues/domain/reasoning/2026-09-09-internal-timeout-misclassified-as-deadline.md) · [REASON-015](../../issues/domain/reasoning/2026-09-10-continuation-context-overflow-progress.md) |
| 主副请求、退款、结构化执行控制、迟回值与用量 | [LLM-041](../../issues/integration/llm/2026-09-06-fallback-window-and-quota.md) · [LLM-042](../../issues/integration/llm/2026-09-07-reserve-r5-cancel-interrupted-rpm-leak.md) · [LLM-043](../../issues/integration/llm/2026-09-08-structured-cancel-deadline.md) · [LLM-044](../../issues/integration/llm/2026-09-08-execution-control-through-every-call.md) · [LLM-045](../../issues/integration/llm/2026-09-09-execution-control-late-result-drop.md) · [LLM-047](../../issues/integration/llm/2026-09-09-deadline-usage-propagation-closed-loop.md) |
| Reflection 真迭代、错误降级及 done 口径 | [REASON-009](../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md) · [REASON-010](../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md) · [REASON-011](../../issues/domain/reasoning/2026-09-01-reflect-done-token-caliber.md) |
| ReAct 早期异常与恢复修复 | [Reasoning 问题索引](../../issues/domain/reasoning/README.md) · [SHARED-001](../../issues/shared/error_handling/2026-08-30-handler-exception-defense.md) |
| 策略抽离、Planner、Reflection、双通道 | [ReAct 抽离 ADR](../../adr/domain/agent/2026-08-27-react-strategy-extraction.md) · [Planner ADR](../../adr/domain/reasoning/2026-09-05-planner-strategy.md) · [Reflection ADR](../../adr/domain/reasoning/2026-08-31-reflection-strategy.md) · [双通道 ADR](../../adr/domain/reasoning/2026-09-04-react-stream-channel.md) |
| 上下文、成本、最终答案、工具反馈 | [语义预算 ADR](../../adr/domain/reasoning/2026-08-28-context-budget.md) · [请求预算 ADR](../../adr/integration/llm/2026-09-06-request-context-budget.md) · [成本 ADR](../../adr/domain/reasoning/2026-08-30-cost-limit.md) · [结构化答案 ADR](../../adr/domain/reasoning/2026-08-28-structured-output.md) · [工具反馈 ADR](../../adr/domain/reasoning/2026-08-27-tool-error-feedback.md) |
| 工具组件、校验与整体审查 | [六组件 ADR](../../adr/integration/tools/2026-08-17-six-component-alignment.md) · [jsonschema ADR](../../adr/integration/tools/2026-08-17-jsonschema-strict-validation.md) · [TOOLS-049](../../issues/integration/tools/2026-08-20-code-review-fixes.md) |
| 共享类型、统一异常与 TokenCounter 演进 | [标识类型 ADR](../../adr/shared/types/2026-08-24-type-identifiers.md) · [异常树 ADR](../../adr/shared/exceptions/2026-08-24-exception-hierarchy.md) · [异常边界 ADR](../../adr/shared/exceptions/2026-08-28-exception-system-optimization.md) · [TokenCounter 已替代 ADR](../../adr/integration/llm/2026-08-24-token-counter-port.md) |

更早 RateLimiter 审查和 reserve/settle 的完成清单只保留其现行[限流组件说明](../integration_doc/llm_doc/limiter.md)作为入口；旧 `app/services/` 路径、重复“待评审”空节和逐轮测试数不另存一份。
