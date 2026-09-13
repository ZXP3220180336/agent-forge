# 项目待办

更新：2026-09-13。本文件只维护尚未关闭的工作记录；已完成工作的独特交接信息见[完成记录](history/completed-work.md)，具体缺陷与决策以当前 `issues/`、`adr/` 为准。执行流程只引用[项目工作流](engineering/project-workflow.md)，运行时判断只引用[运行时规范](engineering/agent-runtime-rules.md)。

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
| C-11 | 其他非关键观测入口的异常与阻塞边界。 | LLM 调用日志已由 [LLM-049](../issues/integration/llm/2026-09-12-llm-observation-overrides-terminal.md) 实施有界隔离；其他日志、指标和审计入口若进入终态路径，仍须逐入口核验 G0-6，不能把局部实现宣称为全仓完成。 |
| C-12 | 流式续接退避期间的取消出口可能与其他主要取消位置不一致。 | 待复现并核对 `streaming_rectifier.py`：续接退避期间的 `_StreamCancel` 据报未转换为取消 SSE，而同一 `cancel_event` 在其他位置会正常结束并产生取消事件，可能导致 `LLMService.async_generate` 随取消时刻分别表现为取消 SSE 或抛出 `LLMCancelledError`。进入实施前须明确公开取消契约，覆盖首次请求、流读取、整流、续接退避及续接请求各时点，并验证事件、异常、流关闭与 usage/Reservation 结算一致；不得仅在单一捕获点吞异常。 |
