# 已完成工作的交接记录

整理日期：2026-09-21。本文件只保留原 `docs/todo.md` 中尚无独立 Issue/ADR 完整承载的有用交接结论，以及指向现行记录的索引。它不是规则正文，也不是旧 todo 全文存档。

原记录中的测试通过、提交和完成状态仅代表当时记录；治理迁移收尾未重跑这些历史业务测试。尚未关闭的事项只维护在[项目待办](../todo.md)。

<a id="refactoring-plan"></a>

## 2026-09-21：R-01 代码职责与编排边界重构归档（R1～R6）

2026-09-14 立项，目标是降低领域推理、LLM 调用与聊天主链路的阅读和修改成本；用户先后授权 R1～R4 与 R6，R5 因 [C-02](../todo.md#c-02-lifecycle) 未闭环被暂缓。范围依据是当时对 `app/` 131 个 Python 文件、18,353 行的静态扫描，据此排出主范围文件（`react.py`、`planner.py`、`reflection.py`、`streaming_rectifier.py`、`llm_service.py`、`structured.py`、`tools/executor.py`、`api/routes/chat.py`）。行数含空行、注释与文档字符串，是该轮快照而非验收阈值，各批完成后均已变化。R5 的结构目标已由 C-02 Piece③④⑤ 一并交付，故 R-01 整体关闭。

| 批次 | 交付与现行记录 |
| --- | --- |
| R1 结构化纯逻辑 | 新增 `structured_codec.py`，`structured.py` 保留调用、usage、降级与错误边界；[LLM-ADR-017](../../adr/integration/llm/2026-09-14-structured-codec-boundary.md) |
| R2 请求执行 | 新增 `request_execution.py`，Facade 保留公开通道与最终结算；[LLM-ADR-018](../../adr/integration/llm/2026-09-15-request-execution-boundary.md) · [组件说明](../integration_doc/llm_doc/request_execution.md) |
| R3 单流消费 | 新增 `stream_consumption.py`，整流器保留预算、续接与唯一结算；[LLM-ADR-019](../../adr/integration/llm/2026-09-15-stream-consumption-boundary.md) |
| R4 策略纯边界 | 新增 `_planner_steps.py`、`_react_protocol.py` 与 Reflection 阶段方法，未引入共享 State 或 Policy；[策略纯边界 ADR](../../adr/domain/reasoning/2026-09-16-strategy-pure-boundaries.md) |
| R5 工具与批次 | 未单独执行，结构目标由 C-02 Piece③④⑤ 兑现（`admission.py`、`execution.py`、`executor.py` 的准备/尝试/完成分段、`tool_batch.py`）；状态见 [C-02 Piece③④⑤ 规格](../todo.md#c-02-implementation-pieces) |
| R6 聊天用例 | 新增 `app/application/chat/chat_service.py`，路由只保留 HTTP/SSE 与断连适配；[ChatRun ADR](../../adr/application/chat/2026-09-16-chat-run-owner.md) · [CHAT-001](../../issues/application/chat/2026-09-16-current-message-duplicated.md) |

各批历史验证数为 R1 定向 62／全量 1046、R2 91／1204、R3 89／1213、R4 250／1230、R6 73／1247，均为当时记录，治理收尾未重跑。

当时确认必须保留的契约差异（各自已有 ADR 承载，此处只留结论）：Planner 仍按串行步骤执行、不引入 DAG 调度，Planner 与 Reflection 的报告 schema 同构但独立发布；三策略的空输出、LLM 失败、协议修复、停滞与重规划计数各有语义，不合并为统一 RetryPolicy；Reflection 自查通过后的 after-turn 取消与 strict deadline 语义不同（[ADR-003](../../adr/2026-09-12-sdk-call-guard-response-commit.md)）；协议与 SSE 提交仍由原责任层控制。

范围治理另评估了两个次级候选与三项可选重构（`session_manager` 481 行、`container.py` 383 行、大型测试文件整理）以及若干「保留」「不纳入」判断，均未纳入执行，触发条件未重新确认，当前不进入候选清单；Memory 与 CoT 的进度另由 [L-01](../todo.md#l-01-domain-layer) 维护。

R4 复核另列出四项既有边缘行为未纳入该轮（Planner 空描述步骤的依赖编号、`final_answer` 与普通工具混用、反向 `finish_reason` 不一致、畸形 function 载荷），当时理由是它们没有已确认的新行为契约；边界与理由见 [策略纯边界 ADR](../../adr/domain/reasoning/2026-09-16-strategy-pure-boundaries.md)。后续两项已另行关闭：畸形调用结构由 2026-09-20 D4 修复（缺 `function` 的调用按未注册工具失败回喂），`final_answer` 与普通工具混用由 C-02 Piece⑤ 在写协议历史前拒绝；其余两项至今没有独立行为契约，需要时按新任务重新确认。

R-01 期间的实际纠正均归入正式位置：异步生成器逐层关闭传播见 [LLM-ADR-019](../../adr/integration/llm/2026-09-15-stream-consumption-boundary.md)，聊天历史快照边界见 [CHAT-001](../../issues/application/chat/2026-09-16-current-message-duplicated.md)，ASGI `spec_version` 2.3/2.4 双分支覆盖与 R1 临时目录经验见 [lessons](../lessons.md)。各批独立复核的细节只保留在其 ADR 的验证章节。R-01 未改变公开契约、配置、部署或模块映射，因此无需修改 ALIGNMENT。

<a id="s-01-schema-dialect"></a>

## 2026-09-15：本地 JSON Schema 版本统一（S-01/S-02）

结构化输出、ReAct 最终答案与工具参数三级本地校验已统一为固定 Draft 2020-12，共享预检与校验器工厂集中在
`app/shared/json_schema.py`：同一 Schema 的版本语义不再随入口变化，缺省即 2020-12，其他版本声明（含子
Schema）拒绝，只支持内嵌及 fragment 引用，显式空 Registry 禁止远程获取。定义错误与实例错误分开——
structured 记 ERROR 后返回 None，ReAct 注入 final_answer 前抛 `SchemaError`，工具校验转问题列表，工具
导出直接抛 `SchemaError`。定义预检收敛在唯一注册入口 `ToolRegistry.register`，使内置装配的逐工具兜底与
外部加载的文件级回滚各自生效；外部工具回滚名单按「已取得资源」而非「已注册」建立，注册失败实例的
`on_load` 资源也被释放。

用户后续复审另修两项：引用目标不再重复元校验（10 个引用/每定义 40 字段本机均值 148.9 → 81.9 ms，约
45%）；`ParameterValidator.validate` 仅在有效 Schema 未变时复用原校验器。内置 Schema 当前都不含引用，
预检成本主要落在外部扩展路径。

决策见 [ADR-004](../../adr/2026-09-14-json-schema-dialect.md)；根因记录见
[SCHEMA-001](../../issues/shared/json_schema/2026-09-14-reference-scope.md)～
[SCHEMA-004](../../issues/shared/json_schema/2026-09-15-repeated-meta-validation.md) 与
[TOOLS-051](../../issues/integration/tools/2026-09-15-registry-schema-preflight.md)～
[TOOLS-053](../../issues/integration/tools/2026-09-15-effective-schema-preflight.md)；提交 `3d4a271`。
本轮全量 1204 passed，`scripts.verify_alignment` 与 `git diff --check` 通过，唯一告警为既存
Starlette/httpx 弃用提示。已知边界：声明旧方言的外部插件会在注册期被拒并回滚本文件，插件作者需迁移。

<a id="d-01-agent-harness"></a>

## 2026-09-15：Agent Harness 架构讲解保存（D-01）

以 [Markdown 正文](../project/agent-harness.md)保存理论架构、项目映射、模拟案例、实现边界与更新约定，
配套 [交互 HTML](../project/agent-harness.html)展示同文内容与原交互；两种格式互链并进入文档导航。
文中「当前」指 2026-09-14 核查时点，后续模块落地后按新证据更新原文，不另建第二份状态表。

未改变源码、模块状态或部署路径，无需变更 ALIGNMENT；本次未运行业务验证。提交 `a379ef1`。

<a id="c-02-p0-design-history"></a>

## 2026-09-13～14：C-02 文档设计交接

初稿、六项定案回填和 P0 规格复核依次完成，设计统一维护在 [TOOLS-ADR-008](../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)。历史各轮完成了对应范围的文档对齐、diff 与链接检查，均未修改运行代码或验证真实数据库/宿主行为。核实出的全局容量语义、会话取消归属、空迁移入口及缺失驱动已纳入实施计划；这只表示文档阶段完成，不表示工具生命周期已实现。当前状态与后续验收只维护在 [C-02 计划](../todo.md#c-02-lifecycle)。

## 2026-09-13：流式业务取消契约统一（C-12）

`LLMService.async_generate` 的业务 `cancel_event` 已统一为类型化终止：Integration 在当前
阶段完成 reservation 补偿或结算、关闭已取得的 SDK stream 后抛 `_StreamCancel`，Facade
翻译为 shared `LLMCancelledError`。Integration 不再生成取消 SSE，也不把取消写入
`StreamResult.error`；终止前的 content、reasoning 与 usage 留在调用方传入的结果载体，
异常同时携带可得 usage。外部 task 硬取消仍传播 `asyncio.CancelledError`，deadline 仍使用
`LLMDeadlineExceededError`，ReAct 独占取消终态事件与 done 的提交。

实现与取舍见 [LLM-050](../../issues/integration/llm/2026-09-13-stream-business-cancellation-contract.md)，
现行生命周期决定见[请求上下文准入 ADR](../../adr/integration/llm/2026-09-06-request-context-budget.md)。
执行控制、Facade、整流与 ReAct 组合回归 210 项通过；全量测试 985 项通过，唯一告警为既存
Starlette/httpx 弃用提示；alignment、Markdown 链接和 diff 检查通过。项目环境未安装 Ruff。

## 2026-09-13：Prompts 模块说明治理

`prompts.md` 已收敛为模块公开契约，补齐包级导出、六个 builder、公共预算语义、错误与
调用方责任，并改用 `app.domain.prompts` 公开入口示例。Reflection 与 Planner 的字段
选择、降采样、省略标记和最小骨架边界分别迁入 `reflection_payload.md` 与
`planner_payload.md` 两份组件说明；模板和简单 Facade 未机械拆文档。

治理同步修正了直接调用方、`PromptTemplate` 使用状态、`REFLECTION_SYSTEM_PROMPT`
接线和 Domain 层组件地图。运行代码、产品、ADR 与 Issue 契约未修改。ALIGNMENT 校验
和文档链接测试 13 项通过，`git diff --check` 通过。

## 2026-09-13：Planner 语义上下文缩减（T-02）

Planner 的 plan、replan、summarize 三个结构化入口已复用 `ContextBudgetPort.count_tokens`
形成阶段性只读载荷。规划保留完整目标和全部工具身份；重规划与汇总按原执行顺序保留
每个步骤的 id、状态、描述与依赖，成功步骤保留结果和工具名/查询参数，失败步骤保留
原因但不作为报告证据。固定 500/4000 字符头部截断已由分层投影和可计数省略标记替代。

`steps_executed` 补充 `depends_on` 审计字段；最小语义骨架仍超限时在 SDK 调用前进入
Context Guard，保留已有 plan、步骤成果和 usage，不做同阶段缩减重试。实施同时修复了
只有失败记录时用列表非空误判部分成功的问题，二维步骤成功与统一 plan 形状保持不变。
实现记录见 [REASON-025](../../issues/domain/reasoning/2026-09-13-planner-semantic-context-reduction.md)，
现行决策见[语义预算 ADR](../../adr/domain/reasoning/2026-08-28-context-budget.md)。专项回归
185 项、全量 984 项通过；唯一告警为既存的 Starlette/httpx 弃用提示；
`scripts.verify_alignment` 与 `git diff --check` 通过。

## 2026-09-13：Reflection 语义上下文缩减（T-01）

Reflection 自查与修正已使用 `ContextBudgetPort.count_tokens` 对阶段性单条载荷计量；
PromptManager 按固定比例缩减 evidence、draft 和 issues，优先保留被引用证据、量测/时间
锚点、结论、置信度、显式放弃及 critical/较新审查意见。省略使用可计数标记，缩减视图
不覆盖原始证据、最近完整稿或 critique。最小提示骨架仍超限时零调用，Integration 最终闸
拒绝时单阶段只调用一次并采用最近稿。

实施问题见 [REASON-023](../../issues/domain/reasoning/2026-09-13-reflection-semantic-context-reduction.md)，
现行决策见[语义预算 ADR](../../adr/domain/reasoning/2026-08-28-context-budget.md)。核心 Prompt/
Reflection 测试 46 项、相关组件测试 64 项通过；最终全量 975 项通过，唯一告警为既存的
Starlette/httpx 弃用提示；`scripts.verify_alignment` 与 `git diff --check` 通过。Planner
后续语义缩减已由上方 T-02 完成记录承接。

同日补充了良率 RCA 的业务对象、报告生命周期和上下文缩减边界，并在 PromptManager
关键决策点记录业务原因。业务语义维护在[产品文档](../project/product.md#良率-rca-的业务对象与报告生命周期)，
Prompt 模块文档只维护接口与缩减契约。

后续治理将 Reflection 动态载荷策略迁入同包私有 `_reflection_payload.py`，PromptManager
只保留公开组装与 Planner 简单序列化。行为刻画同时发现并修复充足预算仍误省略证据的
[REASON-024](../../issues/domain/reasoning/2026-09-13-reflection-evidence-budget-underestimate.md)。
评审确认公开签名、层间依赖和业务优先级均未改变；Prompt/Reflection 相关测试 49 项、全量
978 项通过，`scripts.verify_alignment` 与 `git diff --check` 通过。项目环境未安装 Ruff。

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

原 todo 的旧 Slice 2 未勾选、第一阶段“待提交”、Planner“待实现”以及上下文预算 Slice 3/4 均已被更晚的完成条目覆盖。它们不再进入当前待办；修复依据见下方索引。仍需核验的独立边界见[项目待办](../todo.md)，不在此再复制一份状态。

### 2026-09-01：LLM 错误文档的单一维护位置

错误分类、归一和下游决策说明集中到 [LLM 错误组件文档](../integration_doc/llm_doc/error.md)，retry、Facade、structured 与 streaming 文档以链接引用。原 todo 记载的是文档收敛和 ALIGNMENT 接线过程；错误类型与实现归属的现行决策见[异常归一 ADR](../../adr/integration/llm/2026-09-01-openai-error-normalization.md)及[REASON-010 遗留闭环](../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)。不恢复已撤回的 `shared/error_category.py` 或被删除的旧 ADR。

### 2026-08-30：Facade 导出面与装配根

LLM 包入口曾重导出内部子组件，与“LLMService 为消费方 Facade”不一致；原 todo 记载包入口收窄、包内引用改相对深路径，装配根为配置注册保留内部组件导入。这里保留这一未单独立档的迁移背景；现行接口以 [LLM 模块](../integration_doc/llm_doc/llm.md)和[集成规则](../engineering/integration-rules.md)为准，不将“组合根例外”扩大到业务消费方。

### 2026-08-15：文档镜像迁移与验证基础设施

文档随 `app/` 的分层迁移到 domain/application/integration/infrastructure/shared/platform，建立 [ALIGNMENT](../ALIGNMENT.md) 与 `scripts/verify_alignment.py` 的代码—文档—测试映射。原 todo 记录首批全量 224 passed，补齐测试后 325 passed；这里只保留两个迁移节点，不复制每份文件的操作清单。

当时构建测试替身发现：SQLAlchemy 聚合查询的 `column_descriptions.entity` 也可能是映射类，不能据此区分实体行与聚合；`expr` 与实际查询形状更有判别性。`db_session_factory` 返回值须匹配业务的异步上下文管理协议；SessionManager 的构造参数名不等于实例存储属性名。重复查询前需控制 fake Redis 缓存，否则测试可能未到达目标 DB 分支。配置测试显式关闭 `.env` 读取、工具测试清空真实 API key，以避免依赖本机秘密和真实网络。这些属于已遇到的测试误判机制，使用时仍需核对当前实现。

旧空目录删除曾受沙箱限制；git 不追踪空目录，未影响当时迁移。没有证据证明这些目录今天仍存在，因此不挂为新的清理任务。文档移动后的相对链接、包入口文件名和导入路径均在维护时按实际目录核验；不将早期 `(unknown location)` 导入排查经验解释为唯一根因。

### 2026-09-19：W-01 工具线程所有权与启用边界的交接

W-01 完成后的测试文件分工：工具执行/事实/加载/生命周期替身在 `test_tool_executor_components.py`、`test_tool_fact_ownership.py`、`test_tool_attempt_lifecycle.py`、`test_tool_loader.py`、`test_tools.py`；Agent/策略消费替身在 `test_agent.py`、`test_react_strategy.py`、`test_react_strategy_nonstream.py`、`test_reflection.py`、`test_reflection_agent.py`；审批/选择/注册替身在 `test_tool_approval.py`、`test_tool_selector.py`、`test_tool_registry_metadata.py`（以上均在 `tests/unit/`）。`tests/integration/test_chat_flow.py` 改用真实 readFile，保留完整聊天闭环与迭代预算验证。该分工不使用生产绕过开关或全局 mock 放宽门禁。

该批保留的边界声明：线程取消后仍可能完成已开始的同步工作；不宣称撤销写入、原子写入或跨重启安全。截至该批，注册 10 个内置工具、模型可用 8 个只读工具；WriteFileTool 的受控适配器已修，writeFile / code_exec 的正式执行仍等待交付 B。缺陷闭环见上方完成索引。

### 2026-09-19：W-03 文档复核结论

W-03 的 8 项遗留已逐条复核并修改（7 份文档，无代码改动）：其中 2 项原述不准确、2 项范围被夸大，照单执行会去改并不存在的缺陷。复核方法与已证实的教训见 [lessons](../lessons.md) 的「复用上一轮标注已核实的遗留清单」条目，不再在此重复。

唯一未执行项是 `ToolRegistry` 导出方法死代码的删除。后续复核推翻了它的原述：所称冲突来自六组件对齐 ADR（非 ADR-008）第 22 行，而该行记录的是当时的委托机制，已随 TOOLS-058 把 Facade 导出改为启用过滤自产后失效，Facade 的全量语义未变。删除据此执行并记为 [TOOLS-060](../../issues/integration/tools/2026-09-19-registry-export-dead-code.md)，W-03 至此全部关闭。

### 2026-09-20/21：C-02 Piece⑤ 复核遗留（D1～D5）与 ADR 状态口径修正

Piece⑤ 复核列出的 9 项遗留已全部关闭，**均无独立 Issue**，因此结论写在这里；逐轮验证叙述仍随 C-02 计划维护，见[当前计划](../todo.md#c-02-lifecycle)。

- **回执的操作事实归属改为显式规则**（`react.py` 的 `_aborted_call_outcome`）：执行器每次 attempt 完成都会重发操作事实（`revision=attempt+1`、`attempt_id=None`），在途重试期间它带的是**上一次尝试的旧结果**，所以「操作事实带结果 ⇒ 已确认终局」不成立——重试中被取消的调用会被报成「已确认失败」。现按「在途 RUNNING 的 attempt 快照 > 带结果的操作事实 > 最后一个 attempt 快照 > 操作事实」选取；操作事实内部再按「带结果的优先、同为带结果取最后插入」定归属，业务键复用引入第二条非期望操作事实时不再依赖插入序。
- **批次收尾宽限已配置化**：新增 `tool_batch_cleanup_grace_seconds`（默认 1.0），经 `settings → container.agent_params → AgentContext.batch_cleanup_grace → ExecutionLimits → execute → _handle_tool_calls → execute_tool_calls → ToolBatchRunner.run` 注入；`tool_batch.DEFAULT_BATCH_CLEANUP_GRACE` 只在直接调用方未提供时兜底。此前它是第三个硬编码清理窗口，改配置不影响它，与配置规格及 TOOLS-ADR-008 D7 不一致。取值与约束见[配置参考](../config_doc/config.md#tool-lifecycle-p0)。
- **畸形调用结构不再抛 `KeyError`**：协议判据只校验调用 `id`，缺 `function` 的调用能通过四类判据。新增唯一取值入口 `tool_call_name`（结构缺失归 `unknown`），供停滞指纹、执行取参、final_answer 过滤与停机组装共用。**只修指纹不够**——`_finalize_stalled` 的停机组装是同一处直接下标，只修前者会把崩溃点挪到「连续 4 轮相同畸形调用」。保留原语义：按「工具 `unknown` 未注册」失败回喂模型，不升级为协议类 `PARSE_FAILED`（属契约变更，未做）。
- **宽限耗尽与异常优先级改由测试锁定**：合作取消的任务会被收尾处的一次 `wait(timeout=0)` 捕获（`CancelledError` 写入 `outcomes`），吞掉取消的留在 `pending`、`outcomes` 为 `None`；被捕获者进 `unexpected_errors`，但按固定优先级被 `control_errors` 的三类控制异常掩盖，回执口径不受影响。此前只有注释与一次人工实测，无断言守护。
- **wiring 套件补真实线程覆盖**：新增真实 ToolService + 受控 `invoke`（`execution.run_sync`）走完整 ReAct 批次的用例；取消语义用例仍保留纯协程替身（取消时序比真实线程确定），两者互补而非替代。
- 另删一处恒真断言，并修正[批次组件说明](../domain_doc/reasoning_doc/tool_batch.md)结构图中并不存在的「调用级准入」——`_admission.acquire` 全仓只在 `_request_approval`（仅审批工具）与每次 attempt 两处。

两处 ADR 的**状态陈述与正文矛盾**已修正，只改状态、不改写历史条款：[Agent 错误处理横切入口](../../adr/domain/agent/2026-08-28-agent-error-handling.md) 头部原写「对标增强项 #23（未实现）…实现另行安排」，正文却记同日已实现；[TokenCounter 端口](../../adr/integration/llm/2026-08-24-token-counter-port.md) 原标「已替代」，读起来像尚有未完成条款。前者核对 `app/shared/error_handling.py` 的 `ErrorHandlerRegistry` 与 `AgentErrorKind` 确实存在，后者核对 `domain/ports/token_counter.py` 已不存在、ContextManager 经 `LLMGateway` 计数。

可复用经验：**新增 `AgentContext` 字段必须同时更新测试替身形状**（`tests/conftest.py` 的共享 `agent_params` 与 `test_container.py` 的完整装配断言）。只跑定向测试会漏掉，首轮全量才暴露 17 项。

## 已有 Issue / ADR 的完成索引

下表只导航，不重复展开已经有正式归属的规则和修复过程。

| 批次 / 主题 | 现行记录 |
| --- | --- |
| Schema 方言统一与工具定义预检 | [方言统一 ADR](../../adr/2026-09-14-json-schema-dialect.md) · [SCHEMA-004](../../issues/shared/json_schema/2026-09-15-repeated-meta-validation.md) · [TOOLS-051](../../issues/integration/tools/2026-09-15-registry-schema-preflight.md) |
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
| W-01 写文件真实线程与交付 B 启用边界 | [TOOLS-057](../../issues/integration/tools/2026-09-19-write-thread-ownership.md) · [TOOLS-058](../../issues/integration/tools/2026-09-19-unprotected-tool-enablement.md) |
| W-02 执行声明适配器边界 | [TOOLS-059](../../issues/integration/tools/2026-09-19-execution-spec-boundary.md) |
| R-02 Agent 基类契约与桥接整理 | [AGENT-001](../../issues/domain/agent/2026-09-16-base-contract-drift.md) · [BaseAgent 契约 ADR](../../adr/domain/agent/2026-09-16-base-agent-contract.md) |
| R-03 Reasoning 执行参数语义分组 | [REASON-026](../../issues/domain/reasoning/2026-09-16-execute-parameter-drift.md) · [参数分组 ADR](../../adr/domain/reasoning/2026-09-16-semantic-execution-parameters.md) |
| W-03 遗留容器导出死代码 | [TOOLS-060](../../issues/integration/tools/2026-09-19-registry-export-dead-code.md) |
| C-13 历史查询窗口与文档一致（2026-09-17） | [SessionManager 说明](../application_doc/session_doc/session.md) |
| C-04 无 deadline 时的流读取兜底（2026-09-21 核验已实现，未新增 Issue） | [流消费说明](../integration_doc/llm_doc/stream_consumption.md) |
| C-05 策略层整链硬超时（2026-09-21 核验已实现，未新增 Issue） | [Planner 说明](../domain_doc/reasoning_doc/planner.md) · [Reflection 说明](../domain_doc/reasoning_doc/reflection.md) |
| C-15 `ExecutionLimits` 下界校验（2026-09-21 实施，未新增 Issue） | [reasoning 执行参数说明](../domain_doc/reasoning_doc/reasoning.md) · [Agent 说明](../domain_doc/agent_doc/agent.md) |
| C-24-A `classify_error` 显式识别 `AppError` 树（2026-09-21 实施；B 半场未做，未新增 Issue） | [错误处理契约](../shared_doc/error_handling.md) |

更早 RateLimiter 审查和 reserve/settle 的完成清单只保留其现行[限流组件说明](../integration_doc/llm_doc/limiter.md)作为入口；旧 `app/services/` 路径、重复“待评审”空节和逐轮测试数不另存一份。
