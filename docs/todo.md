# 项目待办

更新：2026-09-12。本文件只维护尚未关闭的工作记录；已完成工作的独特交接信息见[完成记录](history/completed-work.md)，具体缺陷与决策以当前 `issues/`、`adr/` 为准。执行流程只引用[项目工作流](engineering/project-workflow.md)，运行时判断只引用[运行时规范](engineering/agent-runtime-rules.md)。

当前已在完整仓库核对文档迁移，基准提交为 `c351a81`。用户确认本轮仅闭环治理规范与文档迁移；业务源码、测试和运行契约的实现改动暂不处理。下列代码事项保留为后续候选，不作为治理迁移的完成条件；启动时另行确认范围与验收。

## 当前任务：治理迁移收尾（2026-09-12，用户已授权）

- [x] 核对 AGENTS → 规范 → Skill 路由及旧入口要求的迁移完整性。
- [x] 修复 `issues/README.md` 重复导航，接入文档问题索引与总目录。
- [x] 恢复模块说明与 ADR 中可定位的源码引用；各文件仅修改链接，不改变业务契约。
- [x] 更新 `docs/migration.md`、`docs/ALIGNMENT.md`、`docs/project/architecture.md`、`docs/project/deployment.md` 的仓库接入与验证边界。
- [x] 在治理 ADR、文档 Issue、完成记录和 lessons 中记录本次结论，清理活动计划中的已完成批次。
- [x] 验证本地文件链接、锚点、导航可达性、旧路径残留、ALIGNMENT 和差异格式。

可选项：无。本轮不启动 T-01/T-02 或 C-01～C-11，不修改源码、测试、配置或校验脚本。

## 后续代码主线（本轮暂不处理）

| 状态 / ID | 未完事项与收口边界 | 依据与验收入口 |
| --- | --- | --- |
| 待回仓核验 / T-01 | Reflection 阶段语义上下文缩减：evidence、draft、issues 的预算分配与省略标记；缩减不覆盖原始审计证据，仍超限时保留最近可验证稿。 | 原计划“2026-09-10 第二阶段”明确排除 Slice 3，原“上下文预算跨策略闭环”Slice 3 保持未完成。读取[语义预算 ADR](../adr/domain/reasoning/2026-08-28-context-budget.md)、[请求准入 ADR](../adr/integration/llm/2026-09-06-request-context-budget.md)、[Reflection 模块](domain_doc/reasoning_doc/reflection.md)。若进入实施，覆盖超长证据/稿件/问题清单、拒绝后无额外请求、证据引用保留，并遵守 [Reflection 局部契约](engineering/agent-runtime-rules.md#reflection-exception)。 |
| 待回仓核验 / T-02 | Planner 规划、重规划、汇总的语义上下文缩减：保留目标、依赖、成功步、失败原因与证据引用；超限时保留已完成步骤并形成符合既有契约的部分结果。 | 同批明确排除 Slice 4，原 Slice 4 保持未完成。读取[Planner ADR](../adr/domain/reasoning/2026-09-05-planner-strategy.md)、[Planner 模块](domain_doc/reasoning_doc/planner.md)和[请求准入 ADR](../adr/integration/llm/2026-09-06-request-context-budget.md)。若进入实施，验证 plan/replan/summarize 各请求路径、usage 单计、二维步骤成功及统一 plan 形状。 |

原计划曾把 Reflection/Planner 五处 `except AppError` 对上下文超限的处理列作 Slice 3/4 阻塞项；后续已完成跨策略 Guard，但材料不足以证明这五条出口也全部闭环。T-01/T-02 恢复时分别核验其真实调用路径，不直接把旧描述登记为新缺陷。两项各自包含相应测试、模块文档、Issue 和 ALIGNMENT 收尾，原通用 Slice 6/7 不再重复建任务。

## 独立边界与候选建设

以下均需先重新确认必要性和执行范围；目前没有在实施的代码任务。候选不是必须实现清单。

| ID | 候选 / 待核验边界 | 保留理由与触发条件 |
| --- | --- | --- |
| C-01 | Planner `REPLAN_SCHEMA.steps` 的 `maxItems` 边界。 | 2026-09-10“三策略重试上限闭环”收尾明确未并入。检查当前 schema 与实际输出规模，确认风险后独立处理；不与协议重试预算混为同一根因。 |
| C-02 | 工具退避对业务取消信号的响应。 | 同一最新收尾明确未并入。核验工具等待入口和现有停止语义；若确有取消后继续等待/执行，再建立独立问题。 |
| C-03 | LLM 重试配置的非负校验。 | 同一最新收尾明确未并入。先核实配置、Manager 与调用入口；不按参数名统一 Tool/LLM 的不同计数口径。 |
| C-04 | 未配置 deadline 时的无限流读取边界。 | 同一最新收尾明确未并入。先核实 idle/read timeout 与主动不限时的产品契约，不能仅凭没有总 deadline 判为漏洞。 |
| C-05 | Reflection/Planner 策略层整链硬超时，以及取消/超时与结构化降级耗尽的结果区分。 | 原执行交接 F 项仍列候选，但[LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)之后，[LLM-044](../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)已推进每次请求和等待的执行控制。需先评估剩余缺口，不直接外包新 timeout 或恢复旧内置 TimeoutError 特判。 |
| C-06 | 默认 system 提示词注入与 PromptTemplate 测试。 | [领域层说明](domain_doc/README.md)仍称 base 待补测试；旧领域计划只剩 `run()` system 注入未做，planning/reflection 模板和 builder 已完成。先确认产品是否需要默认注入，避免改变已有系统提示词优先级。 |
| C-07 | Memory 基座与生产装配、CoT、循环 checkpoint、运行轨迹持久化。 | [记忆模块](domain_doc/memory_doc/memory.md)与[CoT 说明](domain_doc/reasoning_doc/reasoning.md)为预留；[checkpoint ADR](../adr/domain/reasoning/2026-08-30-checkpointer-resume.md)明确未实现；[trace ADR](../adr/domain/trace/2026-08-31-trace-persistence.md)为已决策待实施。出现实际消费方后分别确认范围；不因已有计划自动创建端口、空壳或存储。 |
| C-08 | 精确 provider 计数、语义摘要、调用前成本预留与供应商共享配额映射。 | 原预算计划“可选项”与[请求准入 ADR](../adr/integration/llm/2026-09-06-request-context-budget.md)的升级路径。分别以容量利用率、证据可追溯性、严格成本或供应商真实配额需求触发，不套同一实现。 |
| C-09 | 配置扩展、默认值调优、热更新与多环境配置。 | 2026-08-29 config 文档重构留下研究性 backlog；没有真实消费方、负载或运维证据的默认值建议不保留为目标值。出现明确需求后重新设计，而不是照抄旧数值。 |
| C-10 | 工具选择器向量召回与工具加载/安全边界的后续增强。 | 旧工具重构与[TOOLS-049](../issues/integration/tools/2026-08-20-code-review-fixes.md)有明确延后项；以工具规模、性能、安全边界或真实故障为触发，已完成的审计脱敏不重开。 |
| C-11 | 非关键观测异常隔离与现行传播行为之间的差异。 | G0-6 是治理目标；[REASON-014](../issues/domain/reasoning/2026-09-09-internal-timeout-misclassified-as-deadline.md)与[LLM-046](../issues/integration/llm/2026-09-09-continuation-finish-guard.md)仍记录日志/收尾异常的传播路径。统一解释只维护在[单一治理来源 ADR](../adr/2026-09-12-single-source-governance.md)；本轮未实施代码。后续先区分非关键观测与必要结算，再核验实际传播和部分成果保留，不能把 G0-6 宣称为已实现现状。 |

## 后续开发交接

- 当前源码版本与工作区状态：`c351a81`，治理文档迁移保留在工作区，未提交。
- 本轮选定任务、逐文件切片及验收：尚未选择，不能把全部候选当执行范围。
- 产品测试：本轮未运行；治理迁移的实际检查见下方评审及迁移说明。
- 评审与剩余风险：实施后记录；若发现旧未完项已在源码闭环，更新其对应 Issue 状态并从本文件移除，不新增历史副本。

## 治理迁移评审

本轮按用户确认完成已提供文档的治理搭建与迁移，以上步骤已完成。原入口要求、唯一正文、路由、导航与引用完成核查；实际文档校验通过，详见[迁移说明](migration.md)。未变更业务代码、测试、配置或校验脚本，未运行产品测试、未提交。

外部 `编码规范文档.txt` 来源仍待提供，唯一缺口记录见[文档问题](../issues/documentation/2026-09-12-reference-gaps.md)。它不等同于已提供治理资料尚未迁移，也不被其他文件替代。后续代码主线与候选继续保留，启动时重新确认范围。
