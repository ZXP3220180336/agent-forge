# 研发教训

更新：2026-09-14。本文件保留已经遇到的误判及其识别条件，供同类工作检索；规范正文只维护在 `docs/engineering/`。Issue 的“已修”和旧测试数字均为历史记录，本轮文档维护未重新验证这些业务结论。当前任务状态只见[项目待办](todo.md)，无独立 Issue 的迁移背景见[完成记录](history/completed-work.md)。

## 规则与文档维护

| 触发条件 | 已遇到的误判与经验 | 事实依据 / 正式规则入口 |
| --- | --- | --- |
| 将离线治理资料接入完整仓库或补齐来源 | 资料缺失说明需按当前目标重新核对；2026-09-14 曾只在计划声明规范已找到，遗漏正式来源状态与 Git 跟踪，提交前须核对链接目标也被纳入提交并同步唯一状态记录。治理搭建与代码符合性分别验收，不据此扩大代码范围。 | 2026-09-12 用户明确范围、2026-09-14 计划复核；[迁移引用核对](../issues/documentation/2026-09-12-reference-gaps.md)；[文档证据规则](engineering/documentation/README.md)。 |
| 整合治理规范、移动说明文件或建立来源映射 | 本次用户纠正了并行保留新版规范、旧规范及 sources 快照的方案：同一规则只有一个正式位置，追溯使用现行 ADR/Issue；旧 todo 的历史过程不等于新待办。来源映射不能变成第二份规范正文。 | 本次 2026-09-12 用户纠正；[单一来源 ADR](../adr/2026-09-12-single-source-governance.md)；[项目工作流](engineering/project-workflow.md)。 |
| 同一缺陷出现不同触发条件 | REASON-004/006 的能力侧与数据侧最终合并到一个协议问题；按表现重复建档曾造成同一机制的状态漂移。 | [协议错误 Issue](../issues/domain/reasoning/2026-08-30-protocol-error-empty-tool-calls.md)；[工程规范](engineering/ai-engineering-rules.md)。 |
| 模块、目录或包出口调整 | 旧迁移曾漏掉不带 `docs/` 前缀的相对链接，过早替换通用路径也破坏具体路径；入口文件名、导入和实际目录不一致曾导致 ImportError。检查现有目录比根据错误字符串推定根因可靠。 | [独特迁移记录](history/completed-work.md)（原交接无独立 Issue）；[项目工作流](engineering/project-workflow.md)。 |
| 功能完成后更新文档与 ALIGNMENT | 成本、取消和停滞能力完成后，模块说明仍保留旧构造参数与异常计数；R2 提取删除私有方法后，ADR 和调用图仍称保留薄委托。除路径和链接外，还须对照实际调用点核对文字、图表与接口清单；历史完成标记不能代替当前入口核对。 | [工具文档漂移](../issues/integration/tools/2026-08-20-doc-drift.md)、[R2 文档勘误](../adr/integration/llm/2026-09-15-request-execution-boundary.md)；[项目工作流](engineering/project-workflow.md)。 |

## Agent 调用、结果与终态

| 触发条件 | 已遇到的误判与经验 | 事实依据 / 正式规则入口 |
| --- | --- | --- |
| 一次循环可因多个协议错误再次付费 | 只有总循环上限，或按错误分支分别清零，都会漏掉跨分支恢复消耗；合法工具协议才是该预算的恢复信号。空输出与 LLM 失败不是协议恢复。 | [REASON-017](../issues/domain/reasoning/2026-09-10-tool-protocol-retry-limit.md)；[重试规范](engineering/agent-runtime-rules.md#retry)。 |
| 增加 terminal 工具或停滞指纹 | 全局按 `final_answer` 名字过滤参数曾把不同参数误判为相同行为；只有结构化输出模式下该名字才有终止工具语义。 | [REASON-017](../issues/domain/reasoning/2026-09-10-tool-protocol-retry-limit.md)；[终止工具规范](engineering/agent-runtime-rules.md#terminal)。 |
| 统一策略 Guard 或调整调用后顺序 | 调用前 Guard 负责拒绝下一副作用；调用后必须先接管响应、usage 与资源，再按 Guard 类型决定提交。Reflection 的成本/after-turn 早退仍保留，但 strict deadline 的迟到稿不能标为按时成功。 | [ADR-003](../adr/2026-09-12-sdk-call-guard-response-commit.md)、[REASON-020](../issues/domain/reasoning/2026-09-11-reflection-guard-checkpoint.md)、[REASON-022](../issues/domain/reasoning/2026-09-12-structured-guard-terminal-loss.md)。 |
| 当前轮只有工具意图，随后取消或超限 | 辅助函数单测通过，主循环仍曾无条件覆盖上一轮可见成果；未执行的 tool_calls 不足以证明已有可交付结果。测试需走完整状态迁移。 | [REASON-016](../issues/domain/reasoning/2026-09-10-cross-strategy-guard-priority.md)；[所有权规范](engineering/agent-runtime-rules.md#ownership)。 |
| 为语义缩减分配字段预算 | 原始数据的近似长度可能小于最终序列化文本；局部额度因此被错误截小，即使总预算充足也会省略证据。需求计量必须使用与最终候选载荷相同的表示。 | [REASON-024](../issues/domain/reasoning/2026-09-13-reflection-evidence-budget-underestimate.md)；[语义预算 ADR](../adr/domain/reasoning/2026-08-28-context-budget.md)。 |
| 多条步骤记录进入重规划或汇总 | 对序列化结果整体取头部会切断记录并系统性丢失后部事实；应先保留每条记录的最小因果骨架，再统一降采样。失败调用可用于诊断，但不能因保留工具名而成为结论证据。 | [REASON-025](../issues/domain/reasoning/2026-09-13-planner-semantic-context-reduction.md)；[语义预算 ADR](../adr/domain/reasoning/2026-08-28-context-budget.md)。 |
| Planner 步骤判定或结果结构重构 | 给步骤成功加“无 error”条件改变了既有二维判据；Guard 提前返回也曾绕开 plan 归一。不能从字段名称推定更严格的业务成功语义。 | [REASON-018](../issues/domain/reasoning/2026-09-11-planner-plan-contract-shape.md)、[REASON-019](../issues/domain/reasoning/2026-09-11-planner-step-success-extra-dimension.md)；[项目契约](engineering/agent-runtime-rules.md#project-contracts)。 |
| 流式调用可能在返回前被打断 | 在 await 正常返回后才保存当前轮，导致终态丢失已经出现的 content/usage；异常 usage 与 current usage 是候选来源，不能叠加成两笔。 | [REASON-012](../issues/domain/reasoning/2026-09-09-deadline-current-round-progress.md)；[所有权规范](engineering/agent-runtime-rules.md#ownership)。 |
| 续接请求可能在取得新流前被拒绝 | 续接前清空元数据，使预算拒绝出口丢失旧流成果与用量；旧 LLM-047 教训中的 reset 位置须按更晚修复理解。 | [REASON-015](../issues/domain/reasoning/2026-09-10-continuation-context-overflow-progress.md)、[LLM-047](../issues/integration/llm/2026-09-09-deadline-usage-propagation-closed-loop.md)；[流式规范](engineering/integration-rules.md)。 |
| 把 TimeoutError 归类成总期限到期 | 网络、日志、结算与本层 timeout scope 都可能产生同名异常；仅看类型或当前时钟曾把内部错误标为 TIMEOUT。修标签时还需检查当前成果是否被保留。 | [REASON-014](../issues/domain/reasoning/2026-09-09-internal-timeout-misclassified-as-deadline.md)；[执行 Guard](engineering/agent-runtime-rules.md#guards)、[异常规范](engineering/integration-rules.md)。 |
| 设置内外 deadline 或描述“硬超时” | 同刻触发曾打断 close/settle；timeout 触发取消不等于任意同步阻塞或吞取消代码都会在同刻返回。清理窗口与终止后的副作用需分别核对。 | [REASON-013](../issues/domain/reasoning/2026-09-09-deadline-cleanup-grace.md)；[生命周期规范](engineering/agent-runtime-rules.md#lifecycle)。 |
| handler 可 CONTINUE 或 RAISE | 默认 STOP 掩盖了 CONTINUE 绕过恢复硬限；RAISE 实际立即传播，也不可能再进入注释描述的“全部收集后仲裁”。 | [LLM 失败预算 Issue](../issues/domain/reasoning/2026-08-31-llm-fail-retry-limit.md)、[REASON-016](../issues/domain/reasoning/2026-09-10-cross-strategy-guard-priority.md)；[重试规范](engineering/agent-runtime-rules.md#retry)。 |

## 请求、资源与异常边界

| 触发条件 | 已遇到的误判与经验 | 事实依据 / 正式规则入口 |
| --- | --- | --- |
| 为未知副作用设计隔离与恢复 | 曾把单次结果未知升级为全工具停用并要求人工解锁；对象失败、资源冲突和工具健康必须分别判断。优先限制有证据的冲突范围；容量回收、冲突解除与业务结果确认也不是同一完成条件。 | 2026-09-13 用户纠正及工业参照；[TOOLS-ADR-008](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)、[G0-4/G0-7](engineering/ai-engineering-rules.md#g0)。 |
| 工具重试循环同时包含结果处理或观测 | 成功后的截断或统计异常曾重新执行已经成功的工具；次数预算只是上界，不能证明副作用可重复。真实调用返回后先接管结果，后处理退出重试异常范围；只有适配器明确判定安全时才允许下一次 attempt。 | [TOOLS-050](../issues/integration/tools/2026-09-14-executor-postprocessing-retry.md)、[TOOLS-ADR-008](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)、[重试规范](engineering/agent-runtime-rules.md#retry)。 |
| Schema 版本或包装变化 | 曾因手写引用解析、仅按字典 identity 去重和 `evolve` 沿用旧 resolver，误拒合法定义或放行递归额外字段。使用规范库解析资源，并对最终根重新创建校验器；测试跨 `$id`、共享子定义及递归 `#`。 | [SCHEMA-001](../issues/shared/json_schema/2026-09-14-reference-scope.md)；[共享契约](shared_doc/json_schema.md)。 |
| 引入定义 / 配置类校验 | 校验点与隔离点错位曾把单点配置错误放大成整批故障，并把工具定义缺陷归因给模型参数。校验应放在唯一入口（与内置逐工具兜底、外部按文件回滚的边界对齐）；声明遍历或失败契约要覆盖实际可达的异常形态——自研遍历有界不等于所依赖的库调用有界，库的异常翻译也不全覆盖，只接主异常会留出逃逸路径。 | [TOOLS-051](../issues/integration/tools/2026-09-15-registry-schema-preflight.md)、[SCHEMA-002](../issues/shared/json_schema/2026-09-15-deep-nesting-recursion.md)、[SCHEMA-003](../issues/shared/json_schema/2026-09-15-pointer-resolution-exception-leak.md)；[工程规范](engineering/ai-engineering-rules.md)。 |
| 为多步流程写失败回滚 | 回滚名单曾按「已进入目标状态」建立（如已注册 / 已提交），于是「已取得资源但尚未进入状态」的当前步骤不在名单里，注册期新增可失败点后立即变成资源泄漏。清理名单按**已取得资源**建立；给某步新增失败可能时回头核对名单是否覆盖它。 | [TOOLS-052](../issues/integration/tools/2026-09-15-register-rollback-resource-leak.md)；[G0-3](engineering/ai-engineering-rules.md#g0)。 |
| 一次 Facade 调用内部含 retry/fallback/降级/等待 | 阶段入口有 Guard 并未覆盖内部真实 attempt；新增控制参数只出现在签名，也不证明每条再调用边已经透传。 | [LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)、[LLM-044](../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)；[请求规范](engineering/integration-rules.md)。 |
| 同一业务取消信号跨流式阶段 | 底层既返回取消 SSE 又可能抛异常，会让公开终态取决于取消时刻。资源拥有者应先 close/settle，Facade 统一传播类型化控制信号，领域状态机独占最终用户事件；已产事实不能随取消被抹除。 | [LLM-050](../issues/integration/llm/2026-09-13-stream-business-cancellation-contract.md)；[生命周期规范](engineering/agent-runtime-rules.md#lifecycle)、[请求规范](engineering/integration-rules.md)。 |
| abort 后 factory 吞取消并迟回资源 | helper 的正常返回曾被误当业务成功或直接丢弃；迟回值承载资源所有权，abort 判定与 settle/close 责任并不互斥。 | [LLM-045](../issues/integration/llm/2026-09-09-execution-control-late-result-drop.md)；[请求规范](engineering/integration-rules.md)。 |
| create 已调度而终止、抛普通异常，或退款被取消打断 | 没有成功响应不证明远端未执行；create 一旦调度，未知 usage 也要保守结算。重复退款和过度退款可能虚增可用配额。 | [LLM-042](../issues/integration/llm/2026-09-07-reserve-r5-cancel-interrupted-rpm-leak.md)、[LLM-044](../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)、[LLM-048](../issues/integration/llm/2026-09-12-create-started-ordinary-error-settlement.md)；[请求规范](engineering/integration-rules.md)。 |
| 多次真实响应合并一次返回 | 只回填末次成功漏掉前序消耗，`not target` 又曾把合法空 dict 当无累加对象；实际可得 usage 与不可得消耗要分开，估算不能冒充实际计量。 | [LLM-038/039](../issues/integration/llm/2026-09-02-usage-accounting.md)、[LLM-047](../issues/integration/llm/2026-09-09-deadline-usage-propagation-closed-loop.md)；[流式规范](engineering/integration-rules.md)。 |
| 手动置熔断状态测试 fallback | OPEN 若已过 cooldown 会转 HALF_OPEN，测试实际走主链路；原绿灯因此固化了错误的“共享主窗口”前提。需要验证真正命中的模型与路径。 | [LLM-041](../issues/integration/llm/2026-09-06-fallback-window-and-quota.md)；[请求规范](engineering/integration-rules.md)。 |
| 宣称 best-effort 或新增异常分类 | 搜到 raise 未追踪外层短路曾误判异常是否到达 Reflection；SDK 原始异常越过 Facade 也使领域 AppError 捕获失效。 | [REASON-010](../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)、[异常归一 ADR](../adr/integration/llm/2026-09-01-openai-error-normalization.md)；[异常规范](engineering/integration-rules.md)。 |
| 扩展 handler、日志或审计影响业务收尾 | 非关键观测必须有界并隔离异常，且最终 Guard 后不能再留下可阻塞提交的 await。LLM 调用日志已在共享入口收口；其他观测路径仍需逐入口核验。 | [LLM-049](../issues/integration/llm/2026-09-12-llm-observation-overrides-terminal.md)、[ADR-003](../adr/2026-09-12-sdk-call-guard-response-commit.md)；[G0](engineering/ai-engineering-rules.md#g0)。 |
| 用户可见错误、截断与审计脱敏 | 长度裁剪挡不住前部敏感值；成功 Hooks 也不能覆盖工具未注册、解析/校验失败等审计路径。诊断价值不构成对外保留秘密的豁免。 | [UNKNOWN 脱敏](../issues/domain/reasoning/2026-08-30-unknown-error-redaction.md)、[审计脱敏](../issues/integration/tools/2026-08-19-audit-sensitive-key-masking.md)、[工具六组件 ADR](../adr/integration/tools/2026-08-17-six-component-alignment.md)；[异常](engineering/integration-rules.md)与[工具规范](engineering/integration-rules.md)。 |

## 测试与维护环境

| 触发条件 | 已遇到的误判与经验 | 事实依据 / 正式规则入口 |
| --- | --- | --- |
| 为全量测试指定临时目录 | R1 曾遗漏父目录，并将 basetemp 放入链接检查器排除的 `.pytest-tmp`，使环境失败掩盖测试目标。先核对父目录、权限及被测代码的路径过滤；仓库自检前先登记新模块，不能改断言绕过环境问题。 | 2026-09-14 [R1 验证记录](todo.md#refactoring-plan)；[部署与验证](project/deployment.md)、[项目工作流](engineering/project-workflow.md#verification)。 |
| 根据语法印象或参数名定性 bug | 工具评审曾将 Python 3.14 支持的异常写法误认作 Python 2 残留；head/tail 截断的 marker 是否计入长度亦取决于组件契约。先核对实际解释器与组件约定。 | [工具六组件 ADR](../adr/integration/tools/2026-08-17-six-component-alignment.md)、[完成记录](history/completed-work.md)（细节出自原教训，无独立 Issue）；[工程规范](engineering/ai-engineering-rules.md)。 |
| fake DB、缓存、配置或异步容器测试 | fake 的返回协议、缓存状态和查询判别曾使断言未到目标分支；真实 `.env` 还曾使工具测试意外访问网络。异步 initialize 未等待则产生假启动失败。 | [测试迁移记录](history/completed-work.md)（原交接无独立 Issue）；[项目工作流](engineering/project-workflow.md)。 |
| 涉及时序竞态或文档格式检查 | 时间预算不足会让测试命中提前入口；无限等待会使失败挂起。中文表格列宽应按项目实际 lint 配置核验，不能把旧脚本做法提升为每次必跑的通用要求。 | [LLM-047](../issues/integration/llm/2026-09-09-deadline-usage-propagation-closed-loop.md)、[完成记录](history/completed-work.md)；[项目工作流](engineering/project-workflow.md)。 |

## Schema 预检覆盖与包装

- 标准子树元校验覆盖按对象身份复用，引用访问还取决于资源作用域；二者不能共用去重键。见 [SCHEMA-004](../issues/shared/json_schema/2026-09-15-repeated-meta-validation.md) 与 [E8](engineering/ai-engineering-rules.md#gates)。
- 包装 Schema 不只可能覆盖非法定义，也可能意外修复原悬空引用或使合法引用失效。原定义与有效定义均须保持失败出口，优化只在可证明语义未变时复用。见 [TOOLS-053](../issues/integration/tools/2026-09-15-effective-schema-preflight.md) 与[参数校验契约](integration_doc/tools_doc/validator.md)。
