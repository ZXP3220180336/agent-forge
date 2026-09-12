# Agent 与工作流运行时规范

本文件细化[通用规则](ai-engineering-rules.md)中的 G0 与 G1，不另建 AG0 副本。适用于 LLM、Tool、ReAct、Reflection、Planner 及自动工作流。下述阶段是语义检查点，不要求每阶段创建方法、对象或枚举。

<a id="routing"></a>
## 1. 运行时 Trigger → Gate

| Trigger | 读取与检查 |
|---|---|
| LLM/Tool/structured generation/continuation 调用变化 | R1 生命周期；涉及资源时 R2；涉及工具时 R5 |
| usage、reservation、stream 或响应接管变化 | R2 所有权，R1 调用后处理 |
| retry、refine、replan、fallback 或 SDK retry 配置变化 | R3 预算；实际调用追加 R1/R2 |
| cancellation、timeout、deadline、cost、Guard 优先级变化 | R4 Guard 与取消；R2 事实接管；R6 结果与提交 |
| Tool history、参数、terminal tool 变化 | R5 协议；存在修正重试时 R3 |
| STOP/CONTINUE/RAISE、异常或 terminal outcome 变化 | R6 终态；通用 E6；影响调用时 R1 |

这些检查与 E3/E4/E5/E6 是同一检查的领域展开，不重复生成报告。

<a id="lifecycle"></a>
## 2. R1：外部调用生命周期

默认的语义检查顺序如下；策略适用的检查点与 Reflection 自查早退契约从[项目契约导航](#reflection-exception)按需读取。这里区分下一调用的准入与本次结果的完成语义，不把二者合成一条隐含产品决策：

```text
调用前准入 → 外部调用 → 响应/调用事实接管
→ 最小无业务副作用解析 → usage/资源结算
→ 调用后 Guard → 业务提交或终止
```

- 准入检查当前状态、取消、剩余时间、请求/context、成本和适用预算；需要 reservation 的系统同时确认预留责任。
- 调用中明确 task/request/stream Owner；等待、排队、退避也受取消与 deadline 约束。
- 一旦获得响应、部分输出或失败元数据，先接管有意义的事实，避免 early return 丢失结算依据。
- Guard 前仅做接管、usage 提取和必要解码等处理；解析失败也不能跳过结算。stream 关闭属于有界资源清理，不宣称其物理上无副作用。策略允许的步骤审计记录、计划快照归一与 Reflection 自查早退判定按[对应策略契约](#reflection-exception)执行；这些处理不新增外部调用或工具副作用。
- 正常响应先完成必要结算；在下表指定的调用后检查点按实际 usage 和状态判定。下一业务调用总要重新准入；业务完成按所属策略契约处理，Reflection 自查早退不得擅自套用严格提交模板。
- 调用抛错、流中断或取消未返回完整响应时，走同等责任的异常路径：接管已知事实、保留未知状态、结算/移交恢复、判定终止或允许重试；不假定有完整响应可解析。

流式输出已向用户发出的部分本身是已发生事实。声明流式提交边界与中断语义，不能把最终 Guard 当作撤销已发送内容的机制。每个新的业务调用都重新准入，包括工具批次中的后续调用。

<a id="ownership"></a>
## 3. R2：响应、usage 与资源所有权

对变更涉及的资源，用最短证据说明“创建/取得者 → 当前 Owner → 转移点 → 结算者 → 异常恢复者”。逻辑结算遵守 G0-3，事实保留遵守 G0-4。

未归账响应与已归账的最近可用结果应有不同语义。可以使用现有局部变量和标记，不强制创建两个类。归账成功后及时解除“仍待归账”的责任；如果归账部分完成或结果不确定，不能直接清空依据并声称成功，也不能在 finally 盲目重加。

usage 区分逐块增量与累计快照；最终 usage 替换估计值时只结算差额或更新同一记录。usage 缺失不等于零消耗，估计值与确认值要可区分。远端请求不确定时保留核对责任，不要求系统臆造供应商账单。

reservation 的释放与真实 usage 的记账是不同结算单元。所有成功、失败、超时、取消、解析失败和 early return 路径均应明确资源去向。重复 cleanup 可以 no-op；重试使用新的调用尝试标识，并按逻辑操作契约决定是否复用幂等键。

<a id="retry"></a>
## 4. R3：Retry Budget Gate

逐种识别 transport/provider retry、empty output、protocol correction、tool retry、refine、replan、fallback 和 continuation；不同名称不能掩盖同一次自动重复。

每种机制明确：

- 逻辑操作、尝试单位、计数 Owner、最大值和配置来源。
- attempt 是否包含首次调用。若 max_retries=N，通常总尝试不超过 N+1；若 max_attempts=N，总尝试不超过 N。采用哪种必须明确。
- 何时消耗、何时重置、哪些错误共享预算；计数在所有重试分支一致，不能因错误类型交替而无限重置。
- 可重试/不可重试错误、预算耗尽结果和 STOP 行为。
- 与全局 max_iterations、时间、成本、并发额度的关系。全局上限不能默认代替局部语义；共享预算可用，但须证明每条路径被覆盖且不能重置逃逸，不要求机械创建独立对象。
- 退避和 jitter 是否适用，等待如何中断；不能把完整新 timeout 每次重新赋值而延长绝对 deadline。
- SDK 内部重试与上层重试是否嵌套；据此核算真实调用上界，避免预算倍增。
- 远端已执行但响应丢失时的幂等、去重、核对或停止策略，遵守 G0-7。

测试覆盖 0、1、N 和下一次请求被阻止；同时考虑非法配置、交替错误、重置条件、预算耗尽、取消退避和 SDK 嵌套。仅为一次调用加异常处理不意味着要新增自动重试。

<a id="guards"></a>
## 5. R4：Guard、取消、Deadline 与成本

调用前 Guard 阻止不允许的下一操作；调用后 Guard 处理调用期间出现的变化。二者不能互相替代。多个原因同时出现时，项目须定义统一优先级及终态选择点，不能让不同 Strategy 偶然决定顺序。项目的具体优先级、类型及挂载差异由 [_common](../domain_doc/reasoning_doc/_common.md)和对应策略文档唯一维护；本次变更须验证相应契约。

取消契约可以是 immediate、graceful 或 after-turn。immediate 尽快中断本地执行并收尾；graceful 允许当前原子操作完成结算；after-turn 允许当前约定 turn 完成。必须定义原子操作/turn 边界、请求何时生效和何时提交终态；不能通过漏掉 Guard 实现取消模式。一旦进入终态，任何模式都遵守 G0-2。未采用的模式无需实现。

绝对 deadline 覆盖排队、退避、调用与必要提交检查；每次调用从剩余时间导出局部 timeout。strict deadline 下迟到的成功响应不能标记为按时正常完成，但可以按结果契约保留成果。必要清理另有有界策略，不得无限等待。远端取消不受控时明确其局限并接管迟到事实。

成本准入根据当前账务和适用预留阻止新付费调用；调用后基于真实 usage 判定是否超限。成本 Guard 不能撤销本轮费用。并行调用若要求严格预算，检查共享预留/准入是否有竞争窗口；不把单线程检查当成并发上限保证。供应商 usage 延迟时明确硬上限能力或估计语义，不承诺无法保证的精确预算。

<a id="protocol"></a>
## 6. R5：Tool Protocol Gate

以项目实际 Provider 协议检查工具名、参数、call ID、assistant/tool 顺序和请求响应配对。在发送之前拒绝本地已知非法 history。

检查批量工具部分完成、失败或取消后历史是否仍可合法发送；根据实际协议补充真实错误结果、构造合法恢复上下文或停止，不编造工具执行成功。terminal tool 的唯一性、独占性及其与普通工具共存规则必须由项目契约确定，不能无条件套用。

协议纠正不意味着可以再次执行已经成功的工具。若纠正触发外部调用，执行 R3 并验证预算及副作用处理。

<a id="terminal"></a>
## 7. R6：错误动作、部分成果与最终提交

STOP 关闭后续业务准入，不能落入 LLM/Tool fallback、refine、replan 或付费 summary。CONTINUE/FALLBACK 只在预算和 Guard 允许时执行；RAISE 的抛出层与接收层明确，移除事实上不可达的重复判断。普通总结调用不是 cleanup。

结果可用性与终止原因分别表达：可能有有效候选答案但运行超时。是否标记 success、degraded、partial 由公共契约决定；不强制新增字段或改变现有返回类型，先判断现有表示是否足够。

界定可用答案、draft、critique、plan 与协议中间状态。当前空轮不能无条件覆盖最近有效成果；工具调用描述不自动等于用户成果。只保留契约允许的可用信息，不要求公开内部推理或原始敏感响应。

最终提交前确认必要结算与本策略适用的 Guard 检查点已完成（Reflection 自查早退从[项目契约导航](#reflection-exception)进入正式文档）。最终 Guard 与提交之间避免可阻塞业务操作；若存在 await、异步观测或并发状态变化，检查是否需要再次校验或使用现有同步机制保证终态选择。终态唯一提交，迟到结果只补充事实，不重新开启本次运行。

非关键观测有界、best effort，失败不覆盖主业务结果；强制审计按通用 G0-6 作为业务契约处理。

## 8. Strategy 应用与维护性信号

| 流程 | 审查重点 |
|---|---|
| ReAct | model turn 接管、结算、Guard 后才解释并执行 Tool/terminal/retry；Tool 也执行自己的调用生命周期 |
| Reflection | draft、critique、refine 独立接管；自查通过/达上限先按既有契约早退，需修正才准入；修正返回归账后复查，详见[项目契约导航](#reflection-exception)中的 Reflection 契约 |
| Planner | plan、step、replan、fallback、summary 的调用边界明确；PLAN_FAILED→STOP 后不得转入 ReAct fallback |

多个 Strategy 重复维护 Guard 顺序、终态构造、多个独立重试或大量关联状态时，按通用 G2 审查。共享规则不等于必须创建共享基类；新增 Guard Policy、Retry Policy、Finalizer 或 RunState 仍须通过 E9。

## 9. 按风险选取验证场景

至少为受影响语义选取对应场景，不要求每次运行全矩阵：

| 场景 | 观察证据 |
|---|---|
| 成功响应与取消/超时同时到达 | usage 接管，按取消/截止时间契约确定终态；终态生效后无下一业务调用 |
| 解码失败、归账失败、重复 finally | 事实可追踪、单次逻辑账务效果、恢复责任明确 |
| 流中断或缺失 usage | 已发部分成果与未知用量如实表达 |
| 预算为零、耗尽、错误交替、嵌套 retry | 实际调用次数有界且匹配计数语义 |
| Tool 部分成功后纠正协议 | 不重复副作用，下一请求 history 合法 |
| Planner STOP、Reflection early return | Planner 无隐式 fallback；Reflection 按[正式策略契约](#reflection-exception)执行适用检查点，保留 critique/usage，不误降级合格稿 |
| 日志抛错或阻塞、Guard 后新增 await | 主终态不被覆盖，提交时契约仍成立 |

优先使用假时钟、可控响应与调用计数观察行为，避免以真实睡眠制造不稳定竞态测试。


<a id="reflection-exception"></a>
## 10. 项目契约按需加载

Gate 是开发检查，Guard 是产品运行时门控。以下正式文档持有具体契约，治理文件不重复维护参数、字段和状态表。

| 变更涉及 | 唯一契约位置 | 检查重点 |
|---|---|---|
| Guard 顺序与共享结果 | [_common](../domain_doc/reasoning_doc/_common.md) | 顺序、短路和状态归属；不新增平行枚举。 |
| Reflection 自查与修正 | [Reflection](../domain_doc/reasoning_doc/reflection.md) | 成本与 after-turn 取消保留 REASON-020 早退；strict deadline 迟到结果按 ADR-003 保留事实并进入超时终态。 |
| ReAct 成果、协议与 usage | [ReAct](../domain_doc/reasoning_doc/react.md) | current 与已归账结果的责任；terminal 能力不能仅按工具名推断。 |
| Planner 步骤与 plan | [Planner](../domain_doc/reasoning_doc/planner.md) | 不额外收窄成功判据；正常与早退的公共结果形状相同。 |
| AgentState 与生命周期 | [Agent](../domain_doc/agent_doc/agent.md)、[Executor](../domain_doc/agent_doc/executor.md) | 对照实际转换与对齐记录，不从 error 文案猜状态。 |
| 取消、deadline 与资源迟回 | [执行控制](../integration_doc/llm_doc/execution_control.md) | 调用控制与资源接管分别验证；有界清理不延长业务预算。 |

<a id="project-contracts"></a>
## 11. 参数语义与结果判定的唯一来源

ReAct 的局部恢复预算见 [ReAct 契约](../domain_doc/reasoning_doc/react.md)；Reflection 修正轮数见 [Reflection](../domain_doc/reasoning_doc/reflection.md)；Planner 重规划次数见 [Planner](../domain_doc/reasoning_doc/planner.md)。LLM 次数见 [Retry](../integration_doc/llm_doc/retry.md)，工具次数见 [Executor](../integration_doc/tools_doc/executor.md)。同名参数可能首次计数不同，改动时对照当前契约及配置，不能按名称统一加减一次。默认值不在治理文件维护。

结果可用性、终止原因及降级含义按所属策略正式文档解释。REASON-019 与 REASON-020 是具体语义决定；SDK 调用前准入、调用后事实接管和提交边界由 [ADR-003](../../adr/2026-09-12-sdk-call-guard-response-commit.md) 统一约束。ADR-003 只收窄 REASON-020 的 strict deadline 分支，成本与 after-turn 取消语义不变。非关键 LLM 调用观测已按 [LLM-049](../../issues/integration/llm/2026-09-12-llm-observation-overrides-terminal.md) 迁移；其他观测路径仍须逐入口核验，不据此宣称全仓隔离完成。
