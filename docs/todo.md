# 项目待办

更新：2026-09-16。本文件只维护尚未关闭的工作记录；已完成工作的独特交接信息见[完成记录](history/completed-work.md)，具体缺陷与决策以当前 `issues/`、`adr/` 为准。执行流程只引用[项目工作流](engineering/project-workflow.md)，运行时判断只引用[运行时规范](engineering/agent-runtime-rules.md)。

<a id="refactoring-plan"></a>

## R-01：代码职责与编排边界重构（R1～R4、R6 已完成；R5 随 C-02 暂缓）

日期：2026-09-14。目标：降低推理、LLM 调用和聊天主链路的阅读与修改成本，让编排入口表达阶段与转换，让协议处理、资源与成果接管有明确归属，服务良率 RCA 的拆分、排查与证据报告交付。

**授权边界**：用户已先后授权执行 R1～R4 与 R6；因 [C-02](#c-02-lifecycle) 尚未闭环，用户明确要求暂不执行 R5。本轮只实施聊天用例迁移及必要验证、文档同步，不借 R6 改动工具生命周期。现有 C-02 的决定、进度和授权边界保持独立；本计划不重开 Piece ①②，不将 Piece ③～⑧纳入本次结构重构。

规范入口：[产品](project/product.md)、[工程 Gate 与最小结构变化](engineering/ai-engineering-rules.md#abstraction)、[工作流](engineering/project-workflow.md)、[编码规范文档](engineering/编码规范文档.txt)。2026-09-14 已按用户提供的目录找到并读取编码规范，来源状态统一见[来源核对记录](../issues/documentation/2026-09-12-reference-gaps.md#外部来源边界)；不复制规范正文，也不据此宣称现有代码已全部合规。

### 范围依据与取舍

2026-09-14 静态扫描覆盖 `app/` 下 131 个 Python 文件、18,353 行，其中 9 个文件达到 500 行；结合领域推理、LLM、工具三个子智能体的只读分析，核对主调用者、测试入口及模块契约。行数包含空行、注释与文档字符串，是本轮快照而非验收阈值；没有据此断言业务 Bug 或测试已通过。每批开始前重新核对代码，不依赖固定行号执行。

| 主范围文件 | 本轮结构证据 | 决定与边界 |
| --- | --- | --- |
| `app/domain/reasoning/react.py` | 1,637 行；`execute` 565 行，`execute_tool_calls` 163 行；LLM 通道、usage、工具协议、批次、历史和终态集中。 | 先分离纯协议，再按 C-02 调整批次；运行级成果与终态仍由策略负责。 |
| `app/domain/reasoning/planner.py` | 1,256 行；`execute` 461 行；步骤执行、记录转换、重规划、部分与完整汇总交织。 | 提取纯步骤转换，在原类内整理执行与汇总阶段，保留重规划预算 Owner。 |
| `app/domain/reasoning/reflection.py` | 806 行；`execute` 339 行；稿件、问题清单、修订次数和护栏决策集中。 | 在原文件提取决策和稿件接管步骤，保留已有 `_critique`、`_refine` 调用边界。 |
| `app/integration/llm/streaming_rectifier.py` | 1,020 行；`rectified_stream` 229 行；策略与单流读取、看门狗、chunk 累积、接缝、结算混合。 | 分离单流消费；整流器继续拥有重试、续接和结算决策。 |
| `app/integration/llm/llm_service.py` | 722 行；`_plan_request` 130 行；Facade 混入请求构造、配额选择、请求闭包与 Reservation 转移。 | 提取内部请求执行，保留公开 API、异常翻译、结果通道与成本/计数代理。 |
| `app/integration/llm/structured.py` | 843 行；纯 schema/JSON 处理与降级、截断扩容、校验回喂混合。 | 先提取无 I/O 的解析校验函数，调用及预算继续留在原组件。 |
| `app/integration/tools/executor.py` | 641 行；`_execute_impl` 144 行、`_execute_with_retry` 198 行；准备、许可、尝试、事实与观测收尾交织。 | 合并到 C-02 Piece ③④；采用已有 ADR 的准入与执行接管边界。 |
| `app/api/routes/chat.py` | 196 行；`send_message` 139 行；HTTP/SSE 与会话校验、Agent 创建、运行登记、上下文、结果保存同处。 | 迁移聊天用例到 Application，路由保留传输适配与断连信号转换。 |

以上是结构治理价值排序的候选集合，不是缺陷严重度清单。优先提取有语义的函数或现有类方法；新对象须有独立生命周期、状态所有权或真实变化原因。采用前轮已核查的 [Extract Function](https://refactoring.com/catalog/extractFunction.html) 与 [Extract Class](https://refactoring.com/catalog/extractClass.html) 手法，不新增通用框架。若实施形成新的结构性决定，按记录规范写入对应 ADR；工具结构继续引用 [TOOLS-ADR-008](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)，不另设竞争正文。

### 批次、文件分工与依赖

原建议顺序为 R1 → R2 → R3 → R4 → R5 → R6。R1 作为首批样板；R2 先稳定请求接管边界，再开展 R3。R4 内按单策略拆小提交；R5 沿 C-02 的能力依赖执行。因 C-02 尚未闭环，用户明确暂缓 R5 并先授权 R6；该调整不改变 R5 规格或状态。

除 C-02 已冻结的路径外，新文件名均为建议；每批启动时核对导出约定、引用与最新契约。新文件必须同时接上实际调用者，不创建空壳或临时兼容层；需要修改超过三个文件时，按下表切片逐一列明职责。仅文件迁移造成的内部导入变更与对外契约变化分别审查。

| 批次 / 状态 | 文件与修改目的 | 结构边界与验收重点 |
| --- | --- | --- |
| R1 结构化纯逻辑 / 已完成 | 新增 `app/integration/llm/structured_codec.py`：schema 规范化、JSON 解析校验、错误摘要、回喂消息构造；修改 `structured.py`：导入纯函数，保留调用、usage、降级和错误边界；适配 `tests/unit/test_generate_structured.py`。 | 使用函数，不新建 Codec 类。定向 62 项、全量 1046 项通过；保留三级降级、扩容/回喂预算、最后一次成果解析及短路；见 [LLM-ADR-017](../adr/integration/llm/2026-09-14-structured-codec-boundary.md)。 |
| R2 请求执行 / 已完成 | 新增 `app/integration/llm/request_execution.py`：迁入请求计划、请求 kwargs 与 `_budget_guarded_call`；`llm_service.py` 保留 Facade 编排。 | 定向 91 项、全量 1204 项通过；请求执行契约现由 [request_execution 组件说明](integration_doc/llm_doc/request_execution.md)维护，见 [LLM-ADR-018](../adr/integration/llm/2026-09-15-request-execution-boundary.md)。 |
| R3 单流消费 / 已完成 | 新增 `app/integration/llm/stream_consumption.py`：流读取、chunk 累积、关闭、接缝处理；修改 `streaming_rectifier.py`：显式传入结果、控制信号与看门狗参数；`llm_service.py` 显式传播 Facade 关闭；新增直接测试并适配整流器/请求边界测试。 | 定向 89 项、全量 1213 项通过；消费组件不依赖整流器私有上下文，`StreamParser` 保留 SDK 解码，整流器保留预算、续接和唯一结算；见 [LLM-ADR-019](../adr/integration/llm/2026-09-15-stream-consumption-boundary.md)。 |
| R4-A Planner / 已完成 | 新增 `app/domain/reasoning/_planner_steps.py`：步骤规范化、计划载荷及单步结果的纯记录转换；修改 `planner.py`：显式调用纯转换并保留执行与提交；新增直接测试并回归 `test_planner_agent.py`。 | `_replan_loop` 保持预算归属；子运行继承 deadline、baseline usage 和运行身份，父级在子策略重置前接管事实。见 [领域推理纯边界 ADR](../adr/domain/reasoning/2026-09-16-strategy-pure-boundaries.md)。 |
| R4-B Reflection / 已完成 | 修改 `reflection.py`：提取 critique 后提交判定与完整修正稿接管步骤；新增修正返回同时取消的高层测试并回归 `test_reflection_agent.py`。 | 先接管最近完整稿与 usage 再判护栏；保留 `_critique`、`_refine`、唯一 done Owner，不新增状态类或共享阶段框架。 |
| R4-C ReAct / 已完成 | 新增 `app/domain/reasoning/_react_protocol.py`：工具调用身份检查、final_answer 构造/校验、动作指纹；`react.py` 保留分阶段主循环与协议预算；新增纯协议测试并回归双通道。 | 成果、usage、历史、终态和工具批次继续由现有运行作用域负责；R5 生命周期未迁移。三切片合并定向 250 项、全量 1230 项通过。 |
| R5 工具与批次 / 随 C-02 | `executor.py` 按既定准备/尝试/完成阶段整理；`app/integration/tools/admission.py` 负责共享容量、排队和许可；`app/integration/tools/execution.py` 负责真实执行句柄和有界接管；现有 `app/domain/reasoning/tool_batch.py` 按 ADR 扩展批次边界；`react.py` 接入。 | 对应 [Piece ③④⑤](#c-02-implementation-pieces)，具体规格、文件联动、测试与状态只在 C-02/ADR 维护。结构迁移与新增运行保证分别验收，不能以搬完文件宣称能力完成。 |
| R6 聊天用例 / 已完成 | 新增 `app/application/chat/chat_service.py` 及包入口：会话预检、消息快照、运行身份、Agent 创建、停止和成果保存；路由只保留 HTTP/SSE、断连适配及 ASGI 发送失败时的外层关闭；Container 统一装配。 | Application 不依赖 FastAPI；每个 ChatRun 独立并幂等关闭。历史按 `id < current_message_id` 固定快照；定向 73 项、全量 1247 项通过，不引入通用 AgentFactory，R5 未改动。 |

表中省略目录的 `test_*.py` 均位于 `tests/unit/`。每个小提交同步其实际变化的模块/组件说明；新增模块登记到 ALIGNMENT，父 README 增加导航。新增聊天用例的模块说明与应用层导航随 R6 创建；其余说明沿既有 LLM、reasoning、工具、路由文档更新。公开契约、配置或部署事实未变化时记录无需修改的依据，不为模板增加无事实变化的文档。

R6 预检与流执行分阶段：当前 `chat.py::send_message` 在构造 `StreamingResponse` 前检查会话存在性与用户归属，迁移后必须保留这一时点，不能将预检全部包入惰性异步生成器。不存在/越权仍在响应头发送前返回既有 404/403 错误，且零消息写入、零 Agent 调用；应用用例可先返回已准备的运行输入，再开始消费流，不为此预设新的运行状态类。运行登记后、首个事件前失败以及消费者关闭时的清理也须验证；若当前基线与治理契约不符，先复现并单独处理，不以行为保持为由固化缺陷。

### 契约保护与验证

各批以保持既有运行契约为前提。实施前对实际差异判断 E1/E2/E7/E9；触及调用、结算、异常或控制流时追加 E3/E5/E6/E8，重试变化追加 E4，并按入口读取生命周期与 retry 专项 Skill。不能因名为“重构”省略正确性检查，也不因文件含 retry/budget 就扩大范围。

必须保留以下已确认差异：

- Planner 仍按串行步骤执行，不改为 DAG 调度；Planner 与 Reflection 的报告 schema 同构但独立发布，不合并成公共 schema。
- 三策略的空输出、LLM 失败、协议修复、停滞及重规划计数各有语义，不合并成统一 RetryPolicy。
- Reflection 自查通过后的 after-turn 取消与 strict deadline 语义不同，不能强行统一三策略的调用后护栏。
- 协议与 SSE 提交仍由原责任层控制；停止后不增加业务调用，生成器关闭不额外 yield，已接管工具事实和最近完整成果不得丢失。

| 验证层面 | 每批必要证据 |
| --- | --- |
| 结构 | 主入口按阶段可读；新组件职责与真实消费者明确；无循环 import、万能 State 或大量参数转发链；结算/提交仍有唯一逻辑 Owner。行数下降不是独立验收依据。 |
| 基线与行为 | 先运行对应既有测试建立基线，再补行为缺口；观察结果、错误类型、事件顺序、网络/工具实际调用次数及资源清理。若发现 Bug，先复现再单独修复，不静默改变契约。 |
| 请求与流边界 | 准入拒绝零调用；取消/期限与响应同时到达先接管事实；未知 usage 保守结算；提前退出关闭流；完成后结算/观测失败不触发业务重放；续接接缝及半工具流行为不变。 |
| 策略与工具边界 | 空/失败/末次允许重试；批次部分完成后终止；父子运行成本与事实归并；最近完整稿保留；协议历史合法、done 唯一。沿 C-02 验收真实线程/进程与迟回结果，不以 mock 宣称资源保证。 |
| 应用边界 | 流前不存在/越权的 404/403、零写入与零 Agent 调用；正常 SSE、主动停止、被动断连、同会话多个 run、运行登记后首个事件前失败、消费者提前关闭；消息保存与取消登记清理的顺序和次数可观察。状态码通过真实路由/中间件验证，不能仅直接调用 `send_message`。 |
| 回归与文档 | 按工作流先相关测试、再全量测试；R1～R3 使用 LLM 对应套件，R4 使用策略/Agent、`test_tool_fact_ownership.py`、`test_nested_run_facts.py`、`test_strategy_generator_ownership.py`，R5 沿 C-02 矩阵，R6 使用 chat_flow/container 并补真实 HTTP 预检验证。运行对齐和 diff 检查，记录实际命令、结果及未覆盖项。 |

测试与文档检查命令统一使用[部署与验证入口](project/deployment.md)，不在本计划重复维护命令表。每批形成独立可回退的提交单元，不混入下一批行为建设；回退必须保留调用者、测试和文档的同批一致性。

### 次级候选与不纳入范围

| 候选 / 决定 | 触发与边界 |
| --- | --- |
| `app/application/session/session_manager.py`（484 行）/ 可选 | 先清理非执行示例与重复查询表达，再根据数据访问变化和测试成本评估 Repository/CachePort；完整持久化分层不是本期前置条件，不改查询/缓存语义。 |
| `app/container.py`（365 行，`initialize` 255 行）/ 可选 | 可按基础设施、LLM、工具、应用提取装配方法；R5/R6 只进行必要接线，不自动开展全文件重构或引入 DI 框架。 |
| `settings.py`、`retry.py`、`reservation_limiter.py` / 保留 | 声明式配置或已有清晰对象职责；当前没有足够证据仅按文件体量拆分。 |
| ToolService、Loader、Agent 桥接、提示词预算载荷、工具解析器 / 保留 | 现有职责可辨认；只做所属批次必需联动，不扩建插件平台或共享管理器。 |
| 大型测试文件 / 可选 | 可随对应批次按行为主题整理，保留用例和 fixture 语义；不作为生产重构完成标准，不为搬家增加镜像测试。 |
| Memory/CoT 占位、队列平台、分布式调度、全库配置整理 / 不纳入 | 没有本次主链路需求与授权；不按历史 TODO 或架构蓝图建设。 |

### 进度与评审

- [x] 完成全库结构初筛及领域/LLM/工具并行分析，区分行数信号与职责问题。
- [x] 核对并引用用户提供的编码规范正式文件。
- [x] 登记 8 个主候选、2 个次级候选、逐文件分工、依赖、可选项和验收边界。
- [x] 明确与 C-02 Piece ③④⑤的归属，保留其既有状态与规格正文。
- [x] 完成本轮计划文档的链接、对齐与差异检查。
- [x] 获得 R1 实施授权并核对基线：结构化输出既有 60 项测试通过。
- [x] R1：先补嵌套 schema 双副本与完整回喂/日志分离行为测试，再提取纯 codec。
- [x] R1：定向与全量回归、独立审查、结构文档及对齐检查完成。
- [x] R6：聊天用例迁入 Application，路由收敛为 HTTP/SSE 与断连适配。
- [x] R6：消息快照、首事件前失败、消费者关闭和 ASGI 发送失败回归完成。
- [x] R6：定向与全量回归、独立生命周期复核、结构文档及对齐检查完成。

R1 文件分工：`structured_codec.py` 承担无 I/O 的转换与校验，`structured.py` 保留日志/异常翻译和运行编排，`test_generate_structured.py` 保护公开调用行为；`docs/integration_doc/llm_doc/structure.md` 维护内部边界，`llm.md` 与集成层 README 更新结构导航，ALIGNMENT 登记新模块；LLM ADR 及索引记录函数模块分离的取舍，本计划追踪执行证据。校验器异常的日志责任留在原模块，codec 不引入 logger/配置/LLM 依赖。后继变更见 [ADR-004](../adr/2026-09-14-json-schema-dialect.md)：三级本地校验已统一为固定 Draft 2020-12（原「前两级 Draft7Validator 与 fallback 的 jsonschema.validate 语义分别保留」条款由该 ADR 替代）。

R1 实施评审（2026-09-14）：采用 E9 的独立数据策略边界，按 E6 核对校验器异常与观测包装；没有新增运行状态、重试或所有权迁移，E3/E4/E5/E7 无新增变化。原类所有方法经引用名归一和去除文档字符串后 AST 与 HEAD 一致，独立代码审查未发现可证实行为漂移。新增源文件为 UTF-8/LF，生产改动行未超过 120 字符；未修改调用方配置或公共 Facade。

实际验证：`.venv/Scripts/python.exe -m pytest tests/unit/test_generate_structured.py -q -p no:cacheprovider` 基线 60 项，补测及拆分后 62 项通过；完整运行 `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Users/Administrator/.codex/visualizations/2026/09/14/01a0a037-f3aa-7fc0-963c-f3fea91e5456/r1-all-tests`，1046 项通过（40.97 秒）。存在一条 Starlette/httpx 弃用提示，不属于本次改动。文档校验使用 `.venv/Scripts/python.exe -m scripts.verify_alignment`，差异检查使用 `git diff --check`，均通过。

验证环境说明：早期全量尝试受到未创建的临时目录父路径、沙箱子进程限制和当时尚未登记的新模块影响；补齐后 1045 项通过，剩余链接测试因临时路径包含检查器排除目录 `.pytest-tmp` 而未进入目标。改用任务临时目录并在允许子进程的环境完成最终全量验证，未跳过测试或修改测试断言；经验见 [lessons](lessons.md)。该说明只记录 R1 验证；R2 结果见本节后续评审。

首次整理验证：`uv run python -m scripts.verify_alignment` 与 `git diff --check` 通过；回填后 `uv run` 遇到缓存访问拒绝，改用已有虚拟环境执行同一检查模块，通过。统计与方法跨度为静态分析证据，未运行业务测试。

再次审核（2026-09-14）：主执行者与独立子智能体对照源码及 C-02，未发现 R2/R3 所有权或 R5 规格冲突；补齐 R6 流前预检、真实 HTTP 验证、首事件前失败及生成器关闭的回归入口。原先只在计划声明编码规范已找到，遗漏了未跟踪文件与正式来源状态：本次将用户原文件纳入提交，更新工程导航、原来源记录及其索引，并将经验补入既有 lessons 条目。未改变模块映射、配置、部署或 C-02 正文，无需更新 ALIGNMENT 或模块说明。修订后运行 `.venv/Scripts/python.exe -m scripts.verify_alignment` 与 `git diff --check` 均通过，另核对 C-02 原文、计划锚点、测试路径和扫描统计，结果一致。上述遗漏已补齐，计划可提交；本轮未运行业务测试，代码实施仍未开始。

R2 文档漂移修正（2026-09-15，用户已授权）：

- [x] 修正 ADR-018 的薄委托描述，明确 Facade 直接调用 `build_request_plan`。
- [x] 同步组件说明的调用图、编排表和包内接口清单。
- [x] 合并文档维护教训，检查残留引用、文档对齐及差异格式并记录评审。
可选项：无；本次只修正文档描述。
评审：已核对两个 Facade 调用点，当前组件说明不再引用已删除方法；旧名称仅保留在
重构前基线及 ADR 勘误中。`scripts.verify_alignment` 与 `git diff --check` 通过。
已新增组件说明并同步层级 README、模块目录、ALIGNMENT 映射及限流交叉引用；纯文档修正未重跑业务测试。

R2 实施评审（2026-09-15）：`request_execution.py` 接管请求参数、计划以及每笔 provider create
的预算、预留、执行控制和调用阶段结算；`LLMService` 保留公开通道、响应接管、最终 usage
结算、异常翻译与事件提交。主请求/续接使用原 `model_key`，fallback 使用独立 `"fallback"`
键和配额池，三者共享单次调用的 `active`；create 前终止仍 `cancel()`，create 调度后未知结果
仍 `settle(None)`，成功或迟回值仍移交通道 Owner。未改变 retry、整流或续接次数。

独立复核逐段对照 HEAD，未发现代码、G0 生命周期或依赖方向问题；发现并修正整流文档两处旧
Owner 表述。定向命令覆盖 `test_llm_service.py`、`test_llm_request_budget.py`、
`test_stream_rectify.py`、`test_streaming_rectifier.py`，91 项通过；全量命令
`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Users/Administrator/.codex/visualizations/2026/09/14/01a0a037-f3aa-7fc0-963c-f3fea91e5456/r2-all-tests`
为 1204 项通过（47.49 秒），仅有既有 Starlette/httpx 弃用提示。首次沙箱内全量运行受 pytest
临时目录和子进程权限限制，结果不作为代码结论；在允许子进程的环境重跑后通过。
`.venv/Scripts/python.exe -m scripts.verify_alignment`、模块编译与 `git diff --check` 均通过。
残余验证边界是 provider 与 Reservation 使用测试替身，未发起真实计费网络调用；R2 是结构拆分，
不需要为验证制造外部副作用。

R3 实施评审（2026-09-15）：`stream_consumption.py` 接管单个 provider response 的逐 chunk
读取、解析后累积、SSE 构造、cancel/deadline/idle 竞争、续接接缝和提前关闭；
`StreamingRectifier` 保留 attempt/续接循环、恢复判定、退避、tool-call 最终合并、熔断、日志、
`active` 与 Reservation 唯一结算。消费函数显式接收结果、attempt 内 tool deltas、控制信号和
看门狗阈值，不读取整流器私有上下文，也不依赖 retry、limiter 或请求执行组件。

竞争顺序保持为已完成 chunk 事实接管 → cancel → deadline → 事件产出；EOF 后仍复查终止，
idle 仅在 chunk 与终止均未完成时成立。非自然退出主动关闭 response，每轮读取与终止等待任务
均 cancel + gather；正常 EOF 继续由 SDK 收尾。新增直接测试覆盖同刻竞争、EOF、idle、消费者
`aclose()` 与跨 chunk 接缝，原整流、请求预算和 Facade 测试覆盖跨组件 Owner。

独立复核先发现外层异步生成器 `aclose()` 不会自动同步关闭正在迭代的子生成器，导致公开链可能
先结算 Reservation、再由异步生成器 finalizer 延迟关闭 response。补高层红测复现后，主流及
`LLMService` Facade、主流、`_abandon_path` 和续接链的每层委托均使用显式 `aclosing` 传播关闭；
Facade、主流与续接三条测试均验证 response close 发生在 Reservation settle 前。复核提出的新文档 EOF 空行也已删除，经验已更新到
[lessons](lessons.md)。修复后再次检查，未发现其余行为迁移、结算、依赖或文档映射问题。

实际验证：R3 定向命令覆盖 `test_stream_consumption.py`、`test_streaming_rectifier.py`、
`test_llm_request_budget.py`、`test_stream_rectify.py`，89 项通过；完整运行
`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Users/Administrator/.codex/visualizations/2026/09/14/01a0a037-f3aa-7fc0-963c-f3fea91e5456/r3-all-tests-facade`，
1213 项通过（44.57 秒），仅有既有 Starlette/httpx 弃用提示。文档对齐、模块编译和已跟踪/未跟踪
文件的差异格式检查均通过。真实 provider 与计费网络调用未执行，结构重构由本地解析、控制与
结算测试保护。按用户要求，本批保留为未提交工作区。

R4 实施评审（2026-09-16）：采用两个包内纯函数组件和 Reflection 原类内两个阶段方法，没有引入
共享 State、Policy 或通用策略执行框架。`_planner_steps.py` 只转换步骤、计划快照与单步审计记录；
Planner 继续拥有串行子运行、replan 预算、父子控制、usage/工具事实接管和完整/部分提交。
`_react_protocol.py` 只处理 final_answer、批内调用身份与动作指纹；ReAct 继续拥有消息历史、协议修正
预算、真实工具批次、Guard、usage 和终态。Reflection 在修正调用归账后先接管非空完整稿及完成
轮数，再执行取消、期限、成本和上下文 Guard；`_critique`、`_refine` 与唯一 done 责任未移动。

结构迁移保持 Planner replan、Reflection refine 和 ReAct 协议修正的既有 0/1/N 上界与重置点；
子 ReAct 继续继承同一 run_id/run_stop/workflow/cancel，并按父级剩余时长与累计 baseline usage
运行。R5 的 ToolBatchCollector、真实工具执行、批次/operation 身份、事实 revision 和协议历史
提交均未迁移。

实际验证：实施前六个策略/Agent 基线 196 项通过；R4 合并定向套件覆盖 Planner、Reflection、
ReAct 双通道、Agent 桥接、父子运行事实、生成器所有权和工具事实，250 项通过。完整运行
`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Users/Administrator/.codex/visualizations/2026/09/14/01a0a037-f3aa-7fc0-963c-f3fea91e5456/r4-all-tests`，
1230 项通过（47.46 秒），仅有既有 Starlette/httpx 弃用提示。最终文档对齐、编译与差异格式
检查通过。独立复核未发现生产代码、G0 生命周期、依赖方向或 R5 越界问题；复核指出并已修正
Planner 旧符号引用、旧 ADR 历史语义改写，以及新 ADR 缺少备选、工业参照与后果的问题。

未纳入的既有边缘项：Planner 空描述步骤的依赖编号、final_answer 与普通工具混用、反向
finish_reason 不一致及畸形 function 载荷。它们没有已确认的新行为契约，不能在纯结构重构中
静默改变；后续若处理，须独立补失败测试并确认预期。

R6 实施评审（2026-09-16）：`ChatService.prepare_message` 在响应头前完成会话 404/403、user 消息
提交、历史快照、run 登记和每请求 Agent 创建；`ChatRun` 成为本次运行、取消登记与 assistant 提交的
幂等 Owner。路由只负责 HTTP/SSE、断连转换、error/`[DONE]` 和传输关闭；`TaskService` 显式关闭
Agent 子生成器。不存在或越权保持零消息、零 run、零 LLM 调用，同会话自然结束只清自己的 run，
公开 stop 与被动断连仍按 session 取消全部活动运行。

基线复现发现旧链先保存当前 user 再读历史，使当前消息进入 LLM 两次；先补失败测试后，以数据库
消息主键 `id < current_message_id` 固定持久化快照。该边界同时排除当前消息和并发期间稍后提交的
兄弟消息，主键缺失或非正数直接失败。独立生命周期复核还发现 ASGI `send()` 失败可绕过 body
生成器收尾；聊天专用响应改在 `__call__` 外层关闭 body iterator 与 run，并验证后续运行可重新取得
TaskService 信号量。两轮复核完成后未发现剩余 P0～P3 问题。

复核后补充（2026-09-16）：ASGI 断连用例原先只覆盖 `spec_version 2.4`，而 uvicorn 声明的是
2.3，Starlette 按 `spec_version >= (2, 4)` 分成两条互不覆盖的路径。现已参数化覆盖两条分支，
并注明判别力差异——2.4 参数证明响应边界清理必要，2.3 参数是路径覆盖与「无悬挂运行 / 许可可
回收」的回归保护（其取消落点取决于生成器是否在 await 中）。同轮登记候选 [C-13](#candidates)
承接 CHAT-001 明确排除的「历史查询取最早 N 条」边界，未改动生产代码。

收尾复核同步修正了 R6 触达代码的注释与 docstring：Container 不再描述由路由构造 AgentContext；
聊天端点按当前 Application/传输职责说明流程；ContextManager、SessionManager 与 TaskService 明确
消息快照上界、有效主键失败和子生成器先关闭再释放并发许可的契约。

实际验证：R6 定向套件覆盖 ChatService、TaskService、ContextManager、SessionManager、Container、
聊天集成与真实 HTTP 端点，73 项通过；完整运行
`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=C:/Users/Administrator/.codex/visualizations/2026/09/14/01a0a037-f3aa-7fc0-963c-f3fea91e5456/r6-all-final`，
1247 项通过（43.39 秒），仅有既有 Starlette/httpx 弃用提示。最终文档对齐、模块编译与差异格式
检查通过。R5 与 C-02 Piece ③～⑧均未修改，本批按用户要求保留为未提交工作区。

<a id="c-02-lifecycle"></a>

## C-02：工具执行生命周期（P0 规格已形成，待实施评审）

目标：从 ReAct 工具编排到 Integration 执行/清理/事实接管，再到领域协议历史和唯一终态，闭合取消、期限、有限重试及未知副作用后的准入。设计唯一正文为 [TOOLS-ADR-008](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)，不在此复制状态与恢复规则。

**授权边界**：用户已确认六项方向并授权完成 P0 规格及实施 piece 的文档拆分；本轮只将已讨论的 piece 写入本计划，不创建运行类、不执行数据库迁移、不提交 Git。P0-A/B 规格已形成；后续按 piece 授权实施，不将计划登记视为 P1～P5 全量代码实施授权。

### 六项定案与交付依赖

六项决策及具体限制统一见 [ADR-008 Decision](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md#decision)：分级持久化、分批验收、code_exec 限制及演进、安全边界协作取消、独立事实收集＋选择性异常传播、单活动进程内多 Agent/多批次共享执行。

保留 P0～P5 编号便于追踪；编号不是必须串行完成的顺序：

- **交付 A（进程内）**：P0-A → P1 → P3-A → P4-A → P5-A。先以普通只读工具验收，不依赖完整执行账本，不承诺崩溃恢复。
- **交付 B（持久保护）**：P0-B → P2 → P3-B → P4-B → P5-B。按工具分级启用，可能有副作用/分类不明/强制调用审计的工具不得跳过必需意图记录和相关保护；复用 A 已完成的控制基础。
- P1 的“成功后处理失败不重放”可在相应修复方案确认后独立实施，不等待完整存储规格；重试安全策略变更仍依赖 P0-A 的适配器契约。
- A 完成只标进程内闭环；B 及相关真实资源/存储测试通过后才标对应持久保护完成，不提前改变未达标工具的保证。

上述箭头表示能力依赖，不要求分别提交不兼容的接口。P3-A 引入 Gateway 必填上下文时，必须同时完成 P4-A 中全部直接消费方、运行身份入口及测试替身的迁移，组成一个可测试的纵向变更；随后再分批扩充并行与终态验收。不以临时可空上下文维持旧调用。

### 本轮文档任务

- [x] 核实 ToolGateway、Executor、ReAct、适配器以及部署/持久化基础；研究结论直接纳入 ADR，不另建调研文件夹。
- [x] 主 ADR：完整生命周期、状态/竞态、逐工具能力、资源保护、恢复限制及工业参照。
- [x] 工具 ADR 索引与旧 004/006/007 承接指针：保留有效条款，标明迁移未完成。
- [x] 本计划：拆分文件职责、依赖顺序、验收与未授权候选。
- [x] lessons：记录“单次结果未知不等于全工具失效”的已纠正设计误判。
- [x] 文档链接、对齐和 diff 检查；实际结果见本轮评审记录。
- [x] 六项讨论定案回填 ADR/todo：撤销全调用强制意图落库及全量账本前置依赖，补多 Agent 隔离、选择性异常传播与未来升级路径。

### P0：最小端口与部署规格（已形成；Piece①、②已实施）

唯一详细规格见 [ADR P0 S1–S8](../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md#tool-lifecycle-p0-spec)，初值见[配置规格](config_doc/config.md#tool-lifecycle-p0)，运行约束见[部署规格](project/deployment.md#tool-lifecycle-p0)。本节表格保留交付职责导航，不重复字段和参数值。

- [x] P0-A：公开签名、独立 facts 端口、共享异常出口、运行/批次/attempt 身份与状态迁移。
- [x] P0-A：Executor、Admission、Supervisor、BatchRunner/Collector 的方法及文件边界与 E9 依据。
- [x] P0-A：多 Agent 公平准入、实际消费者、同会话多 run 取消归属、禁止未支持嵌套执行。
- [x] P0-B：逐工具持久化/资源等级，保守目录范围与 code_exec 限制。
- [x] P0-B：SQLAlchemy 专用账本、CAS/事件幂等、SQL 迁移和 asyncpg 依赖接入位置。
- [x] P0-A/B：配置初值、Windows 单机 Owner、宿主关闭机制和验证矩阵。
- [x] P0 规格已通过实施评审；Piece①、②已按纵向切片落地，后续运行保证仍按 Piece③～⑧逐项验收。

本阶段先补规格，不创建占位类。对现有消费者逐一映射：

P0-A 冻结运行/批次/调用身份、独立事实入口、类型化终止、父子取消与期限、共享容量和有界排队、协作检查点及只读能力验收。P0-B 冻结持久化等级、账本迁移、恢复准入、资源身份和必要的启动排他/强制退出机制；不能把全部 P0-B 作为只读交付 A 的前置条件。

| 文件/正式位置 | 工作内容 |
| --- | --- |
| `app/domain/ports/tool_gateway.py`（设计映射） | 确定控制参数、操作/尝试身份、结果及控制出口，必要事实字段与默认未知语义；保留既有调用次数口径 |
| `app/integration/tools/base.py`（设计映射） | 确定可信重试安全、资源范围、可选只读核验及执行结束证明的最小能力，避免为每个语义增加独立 Policy |
| ADR-008 | 补端口字段与退出形状、各运行独立事实收集、资源层级冲突表、逐工具持久化等级及 A/B 支持集合；不重开已定六项方向 |
| `docs/project/deployment.md`、`docs/config_doc/config.md`（规格确认后） | 明确单活动进程/存储范围、启动排他方式、schema 初始化与回滚；确定有界清理、接管容量、记录及核验重试配置，不复制多份默认值 |

上述原待冻结细节已在 ADR S1/S6/S7、配置与部署规格逐项给出明确方案。未来平台级升级仍按 D11/D12 触发，不能混入 P0 未完成项。初值尚无负载实测，DB/进程退出保证待 B 实施验证，不能把规格完成写成运行验收完成。

部署核验结论：目前只有本地启动证据，没有生产 worker/共享文件系统声明。首期支持单活动执行进程内多 Agent、多批次并发，实际共享资源统一准入，取消与事实按所属运行隔离；不把进程内锁声称为分布式保证。如目标必须多实例共享执行，先调整部署决策。Agent 收尾有界与整个应用退出有界分别验收。

验收：每个公开出口有返回/抛出及事实去向；每类工具有支持/限制结论；所有等待和后台工作有上限来源；架构依赖通过审查。

<a id="c-02-implementation-pieces"></a>

#### P0 规格的实施 piece

P0 S1～S8 是设计职责划分；下列 piece 是实施与验收单元，沿用 P1～P5 的任务归属。字段、状态及默认值以 ADR/配置规格为唯一正文；具体文件分工沿用下方对应 P 阶段，不在此复制。Piece①、②已兑现，后续 piece 仍待实施；局部完成不代表整套生命周期闭环。

| Piece / 当前状态 | 范围与实施归属 | 主要规格依据 | 完成条件 |
| --- | --- | --- | --- |
| ① 调用前后处理与安全重试 / 已完成 | P1：已分离真实执行、结果处理和非关键观测；BaseTool 默认禁止重试，只有显式安全声明和次数余额同时满足才重复；见 [TOOLS-050](../issues/integration/tools/2026-09-14-executor-postprocessing-retry.md) | S2、S3、S8 | 红测闭合；工具相关 94 passed、全量 991 passed。生产适配器与 SDK 内部重试上界仍归 Piece ④逐项核验，不影响本 piece 的 Executor 边界验收 |
| ② 运行身份、控制信号与独立事实 / 已完成 | P3-A＋P4-A：已实现公开上下文、事实入口和类型化终止；Application 创建 run_id，贯穿 Gateway、Agent、策略及测试替身 | S1、S3、S4、S8 | 强制 call/facts 契约、同会话多 run 取消隔离、批次事实先接管和 shared 异常依赖方向已由专项与跨层测试覆盖；真实后台句柄仍按 Piece ④边界待实施 |
| ③ 共享准入与配置迁移 / 待实施 | P3-A：实现 Admission 的全局/单运行容量、有界排队、按运行轮转和资源联合准入；同步配置字段、Container、settings.py 注释及配置文档 | S3、S5、配置规格 | 容量不超限；撤回与取得许可竞态不泄漏；无关运行可继续；旧键无兼容别名，改名不扩大总并发 |
| ④ 真实执行与有界清理接管 / 待实施 | P3-A：实现 Supervisor、AttemptHandle 及首批只读适配器的真实完成跟踪、清理和迟回事实接管 | S2、S3、S5、S6、S8 | 真实线程未结束不退容量；迟回值有 Owner；清理与接管有界；依赖不在仍被使用时提前关闭 |
| ⑤ 并行成果、协议历史与终态 / 待实施 | P4-A＋P5-A：在 ② 的事实接线上完善 BatchRunner/Collector、ReAct 历史及事件提交，完成只读交付 A 验收 | S1、S4、S8 | 部分完成后取消仍保留兄弟成果；协议消息正确配对；done 唯一；终态后无新业务调用 |
| ⑥ 持久意图、事实账本与恢复 / 待实施 | P2：实现存储端口、数据库适配器、迁移和恢复接线；覆盖条件更新、事件幂等、意图比较及累计核验次数 | S3、S7、S8 | 必需意图写入失败时零执行；旧事实不覆盖新事实；重启不重放业务、不重置核验次数；通过真实数据库验证 |
| ⑦ 副作用工具与资源保护 / 待实施 | P3-B＋P4-B：逐个接入文件、HTTP、code_exec 的持久记录、效果声明和资源保护，保留各适配器能力限制 | S2、S5、S6、S7、S8 | 部分/未知效果如实记录；冲突调用被阻止，无关资源可继续；未达到持久化与部署前提的工具仍禁止启用 |
| ⑧ 启动恢复、宿主退出与整体验收 / 待实施 | P5-B，配合 P2/P3-B：完成 Windows Owner、受支持宿主及真实进程验证；同步组件、配置、部署与总验收证据 | S3、S7、S8、部署规格 | 启动先恢复保护再开放；强退与重启边界经真实验证；数据库、线程/进程及全量回归通过后，才确认 B 的支持范围 |

建议执行顺序：① → ② → ③ → ④ → ⑤（交付 A）→ ⑥ → ⑦ → ⑧（交付 B）。这是验收推进顺序，不允许以未实现的依赖或空壳替代可运行接线：② 的新签名与消费者必须在一个可测试的纵向变更中完成；⑦ 可在受控测试中接线，但副作用工具正式启用须满足 ⑧ 所验收的启动/部署条件，必要前置实现随 ⑥/⑦ 同批完成。

每个 piece 开始时核对最新代码，按既有 P 阶段文件表拆成小任务；缺陷先写红测，新增组件必须接上真实消费者。完成后更新本表状态并链接实际验证/Issue，不把代码已写等同于验收通过。沙箱、分布式准入及其他可选能力仍按“可选项与未授权范围”管理，不随 piece 自动扩展。

### P1：建立失败复现并分离执行与后处理

先红测，之后仅按已批准契约修改；后处理异常范围修复与安全重试策略分成小任务，后者依赖 P0-A，前者不等待 P0-B/P2：

| 文件组 | 修改目的 |
| --- | --- |
| `tests/unit/test_tool_executor_components.py`、`test_tool_hooks.py`、`test_tool_audit.py` | 复现成功后截断/统计/审计异常不得再次执行；普通失败与控制终止分道；挂起观测有界 |
| `app/integration/tools/executor.py` | 真实调用与结果处理的异常范围分离；保留成功/部分事实；按安全声明限制尝试 |
| `app/integration/tools/hooks.py`、`security.py`、`result_processor.py`（按实际触点拆小提交） | 盘点 Hook 的业务性质；非关键观测有界隔离；展示裁剪不覆盖恢复事实 |
| `issues/integration/tools/`及所属索引 | 红测证实后按独立根因登记，不预先标已修复或一次生成所有 Issue |

验收：SDK/工具执行计数不因后处理失败增加；0/1/N 计数保持明确；永久错误无盲重试；无观测异常覆盖已接管结果。

**复审遗留**：适配器返回非 `ToolResult` 时，本次改动已把 `execution_time` / `retry_count` 填充与 `result.success` 读取移出任何异常分类范围，`AttributeError` 会逃逸 `ToolExecutor.execute`，再经 `react.py` 无 `try` 的 `_execute_one` 中断整批工具调用（探针复现；改动前由外层 `except Exception` 收编为 `UNKNOWN` 失败结果）。外部工具经 `loader.py` 动态收集，属信任边界。修复方向：真实调用返回后先按 `isinstance(result, ToolResult)` 收编为 `UNKNOWN` 失败（不进入重试），再填充执行元数据。触发条件：Piece ④ 为适配器开启安全重试前，或外部工具接入验证时。

### P2：持久事实垂直切片与装配

属于交付 B，依赖 P0-B；不是普通只读交付 A 的前置条件。按 D8 分级接入，既有证据保存要求独立核验，不以无需意图落库推导结果可丢弃。

| 文件组 | 修改目的 |
| --- | --- |
| `app/domain/ports/`（P0 确定专用存储端口文件名） | 只定义实际需要的登记/条件更新/回执/未决读取能力，不建设通用 Repository 框架 |
| P0 已定位的新路径 | `domain/ports/tool_execution_store.py`、`infrastructure/tool_execution_store.py`、`infrastructure/models/database/tool_execution.py`；方法/表结构见 ADR S3/S7 |
| `app/infrastructure/models/database/`及专用存储适配器 | 实现操作/尝试账本与原子状态更新；不得以空 tool_log.py 已存在认定存储已实现 |
| schema 初始化/迁移入口（P0 定位后列实际路径） | 明确建表、升级、失败恢复及部署检查；不用现有会话表修改冒充工具存储 |
| P0 已定位的部署入口 | `scripts/migrate.py`、`scripts/init_db.py`、`migrations/tools/0001_execution_ledger.sql`、`infrastructure/tool_owner.py`、`scripts/run_tool_host.py`；此处 app 内路径省略 app/ 前缀，完整归属见 ADR S3 |
| `app/container.py`、`app/integration/tools/tool_service.py` | 注入现有 session 工厂及账本；启动存储核验；接管生命周期在 DB 关闭前完成 |
| 存储单测及 `tests/integration/` 专项 | 验证重复写、旧事件乱序、意图/执行/结果各崩溃窗口、重启扫描，不仅 mock 整个存储端口 |

验收：必需意图写入失败时相关真实执行为零；不依赖该存储且准入满足的只读调用不被统一阻止；成功但必需记录失败只重试记录；并发条件更新不丢事实；重启不重放旧业务；未决记录不依赖内存存活。未完成真实数据库验证不得标记跨重启闭环。

### P3：执行控制、真实资源接管与适配器能力

拆为 P3-A（依赖 P0-A/P1）和 P3-B（另依赖 P0-B/P2）。A 建设进程内控制、共享执行与普通只读接管；B 接入必要持久保护、写工具和重启恢复。A 可以独立验收，但不得冒充完整持久保护。

| 文件组 | 修改目的 |
| --- | --- |
| `tool_gateway.py`、`tool_service.py`、`executor.py` | 透传取消/绝对期限；刷新、审批、容量/资源等待、退避均受控；每次真实调用前复查 |
| `app/config/settings.py`、`app/container.py`、`docs/config_doc/config.md` / P3-A | 同批迁移旧键 agent_max_concurrent_tools 至 tool_max_concurrent_executions_per_run，不保留旧名兼容别名，并接入 tool_max_concurrent_executions 全局上限；同步修正 settings.py 字段注释及配置文档，明确单运行与共享全局容量的区别。验收同时核对字段、装配、注释和文档，不因改名单独扩大总并发 |
| `base.py`与最小控制/接管组件（P0 确定路径） | 等待竞态、任务/许可所有权、后台容量及有限恢复；不复用 LLM 私有异常 |
| `executor.py`、`tool_service.py`、`container.py`与 P0-A 确定的最小准入组件 | 多 Agent 共享全局/单运行容量、有界队列及简单按运行轮转；资源阻塞不长期占真实执行槽；各运行状态隔离；禁止未支持的网关递归执行 |
| `builtin/file_ops.py`、`builtin/code_exec.py` | 文件部分写入事实、跨工具资源层级、进程及后代结束边界；不能从 Shell 返回推定全部副作用结束 |
| `builtin/search.py`、`builtin/web_browse.py`、`external/http_api.py` | 线程实际结束前保留容量；对象失败不全工具隔离；HTTP 未知写按适配器契约保护 |
| `loader.py`、`tool_service.py`、`container.py` | 实例版本固定；卸载/关闭不抢占在途共享资源；启动恢复保护后才开放准入 |

每组独立红测与小提交；现有 `test_tools.py`、`test_tool_loader.py`、`test_http_api_tool.py`、`tests/integration/test_tool_execution.py` 按实际边界扩展。接管子组件新增测试文件的名称随 P0 结构确定，不预造空文件。

### P4：领域并行事实、协议历史与终态

P4-A 随 P3-A 验收只读并行闭环；P4-B 随 P3-B 补持久回执、未知副作用及恢复引用。采用独立事实收集＋选择性异常传播，不把业务终止统一转换成正常回执，也不强制在异常对象中复制全部批次成果。

文件工具安全放弃点、HTTP 未知写保护及 code_exec 必需意图记录随 B 验收。code_exec 限制与升级方向见 ADR D11；首期不默认实施沙箱、完整进程树隔离或任意命令资源推导，也不将这些能力标为已具备。

| 文件组 | 修改目的 |
| --- | --- |
| `app/domain/reasoning/react.py` | 每个完成调用立即接管；批次取消保留兄弟结果；主生成器统一事件；终态选择前归并事实 |
| `app/domain/reasoning/reflection.py`、`planner.py`及 Agent 消费者（按引用核实） | 透传同一控制边界、吸收已接管成果，不给子跑重置 deadline，不吞类型化终止 |
| 现有 Application 协作调用方及策略构造入口（P0-A 按引用定位） | 父取消向下传播、单运行取消不横向误伤；每次运行独立可变策略状态；不为本期新建协作任务拆分与汇总平台 |
| `tests/unit/test_react_strategy.py`、`test_react_strategy_nonstream.py`及相关策略套件 | 对真实 ToolService/Executor 边界做并行、协议与终态测试，不能只 mock 整个 Gateway 返回 |

验收：部分完成后任一路径均不丢事实；没有重复工具执行、重复结果或第二个 done；未执行/未知结果如实配对；下一请求 history 合法；终态后没有新业务调用。

### P5：总矩阵、文档与收口

P5-A 先验收进程内场景；P5-B 验收持久/恢复及受支持副作用工具，并做整体回归。下表按 A/B 实际支持范围选取，不能要求先跑完 B 才交付 A，也不能跳过 B 测试宣称持久能力就绪。

| 场景组 | 必须观察的行为 |
| --- | --- |
| 入口/刷新/审批/容量/锁/持久化/退避中 cancel 或 deadline | 实际 attempt 数；未执行事实；已取许可归还；无后台 waiter 泄漏 |
| 信号与许可/结果同时到达；任务吞取消迟回 | 值有 Owner；许可不丢；结果记录一次；控制终态不变 |
| attempt 内业务取消、局部超时、总期限、硬 Task.cancel | 当前 attempt 边界准确；期限不重置；不同超时来源不误分类 |
| 线程继续、进程未结束、清理超过窗口、队列已满 | 真实容量不提前释放；有界移交；新准入受限；原 Agent 可收尾 |
| 成功后解析/截断/日志/审计/事实写入失败 | 不再执行业务；成功事实不丢；关键记录与非关键观测分道 |
| 并行一成功一挂起、事件 yield 后消费者关闭 | 兄弟事实已接管；无重复 done；结果顺序和协议配对合法 |
| 多 Agent 多批次、一个运行取消或局部超时 | 全局/单运行并发符合上限；无串结果；无关 Agent 继续；父级取消向下覆盖 |
| 大批次持续到达、共享资源阻塞、排队满、嵌套执行 | 有界排队及公平进展；无关资源不被占槽拖住；溢出明确拒绝；无持槽等待子调用死锁 |
| 自有工具在写入前取消、不可拆操作中取消 | 安全边界停止未开始写入；已发生事实保留；不支持协作的工具按声明边界验收 |
| 未知写同资源/无关资源/跨工具/父子范围/路径别名 | 只阻止真实冲突；核验允许；不能靠改 call ID 绕过 |
| 操作成功/失败/部分完成/未知，随后自动或人工恢复 | 容量释放、冲突解除、业务确认独立；无盲目 replay 或补偿 |
| 意图前后/执行后/结果落库前崩溃、旧实例迟回 | 启动恢复保护；不重放；旧状态不覆盖新事实；schema 与排他边界真实验证 |
| 0/1/N、错误交替、SDK 嵌套重试 | 有可证明真实调用上界；安全条件不因预算存在被豁免 |

完成代码后更新工具/端口/ReAct/存储组件说明、配置、部署、ALIGNMENT 和相应 Issue；只同步已兑现的行为。ADR 标记实施证据，旧决策指针按实际迁移状态更新。根级 ADR-003 的通用顺序不重复建正文。

验证命令以 [部署说明](project/deployment.md) 为正式入口。按批次运行工具执行、Hooks、审计、HTTP、资源、ReAct/策略定向测试；最终全量 pytest、真实数据库恢复与受控线程/进程验证、`scripts.verify_alignment`、`git diff --check`。没有服务/平台证据时如实列未验证项，不能用单测数量替代端到端验收。

### 可选项与未授权范围

- 多实例共享资源的分布式准入与 fencing：真实部署需要时先补决策，首期不宣称支持。
- 任意工具跨调用业务语义去重、自动补偿/重放、通用工作流恢复引擎：不在本期。
- 通用工具熔断、管理 UI、插件平台扩建：有产品或故障证据后再规划；本期必要核验/人工记录能力不等于建设管理平台。
- 工具费用精确预留与通用 SDK 自动重试治理：仅核验本次触达工具的真实尝试上界，不扩大到全仓。
- code_exec 沙箱、平台进程树管理、网络出口及持久输出通道，租户公平调度、跨机器执行服务、持久工作流：需求触发与具体限制由 ADR D11/D12 唯一维护；不纳入本期必须完成清单。

### 本轮评审记录

2026-09-14 最新结论：Piece ①、②已完成。Piece ②新增强制 `ToolCallContext/ToolFactSink` 契约，chat/Application 创建唯一 run 身份，TaskService 按 run 隔离且按 session 取消全部活动运行；三种 Agent/策略透传父 run 控制，ReAct 为每批/每 call 建立独立身份并在类型化终止前接管事实；Integration 先保存事实副本再通知 Domain。定向策略/Agent/工具生命周期 232 项、全量 1015 项通过，`scripts.verify_alignment` 与 `git diff --check` 通过。Piece ③～⑧待实施，线程/进程真实句柄、迟回值和有界清理仍归 Piece ④。

历史背景见 [C-02 文档设计交接](history/completed-work.md#c-02-p0-design-history)，不在活动计划中重复各轮验证叙述。

<a id="candidates"></a>

## 独立边界与候选建设

以下均需先重新确认必要性和执行范围；目前没有在实施的代码任务。候选不是必须实现清单。

| ID | 候选 / 待核验边界 | 保留理由与触发条件 |
| --- | --- | --- |
| C-01 | Planner `REPLAN_SCHEMA.steps` 的 `maxItems` 边界。 | 2026-09-10“三策略重试上限闭环”收尾明确未并入。检查当前 schema 与实际输出规模，确认风险后独立处理；不与协议重试预算混为同一根因。 |
| C-03 | LLM 重试配置的非负校验。 | 同一最新收尾明确未并入。先核实配置、Manager 与调用入口；不按参数名统一 Tool/LLM 的不同计数口径。 |
| C-04 | 未配置 deadline 时的无限流读取边界。 | 同一最新收尾明确未并入。先核实 idle/read timeout 与主动不限时的产品契约，不能仅凭没有总 deadline 判为漏洞。 |
| C-05 | Reflection/Planner 策略层整链硬超时，以及取消/超时与结构化降级耗尽的结果区分。 | 原执行交接 F 项仍列候选，但[LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)之后，[LLM-044](../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)已推进每次请求和等待的执行控制。需先评估剩余缺口，不直接外包新 timeout 或恢复旧内置 TimeoutError 特判。 |
| C-06 | 默认 system 提示词注入与 PromptTemplate 测试。 | [领域层说明](domain_doc/README.md)仍称 base 待补测试；旧领域计划只剩 `run()` system 注入未做，planning/reflection 模板和 builder 已完成。先确认产品是否需要默认注入，避免改变已有系统提示词优先级。 |
| C-07 | Memory 基座与生产装配、CoT、循环 checkpoint、运行轨迹持久化。 | [记忆模块](domain_doc/memory_doc/memory.md)与[CoT 说明](domain_doc/reasoning_doc/reasoning.md)为预留；[checkpoint ADR](../adr/domain/reasoning/2026-08-30-checkpointer-resume.md)明确未实现；[trace ADR](../adr/domain/trace/2026-08-31-trace-persistence.md)为已决策待实施。出现实际消费方后分别确认范围；不因已有计划自动创建端口、空壳或存储。 |
| C-08 | 精确 provider 计数、语义摘要、调用前成本预留与供应商共享配额映射。 | 原预算计划“可选项”与[请求准入 ADR](../adr/integration/llm/2026-09-06-request-context-budget.md)的升级路径。分别以容量利用率、证据可追溯性、严格成本或供应商真实配额需求触发，不套同一实现。 |
| C-09 | 配置扩展、默认值调优、热更新与多环境配置。 | 2026-08-29 config 文档重构留下研究性 backlog；没有真实消费方、负载或运维证据的默认值建议不保留为目标值。出现明确需求后重新设计，而不是照抄旧数值。 |
| C-10 | 工具选择器向量召回与工具加载/安全边界的后续增强。 | 旧工具重构与[TOOLS-049](../issues/integration/tools/2026-08-20-code-review-fixes.md)有明确延后项；以工具规模、性能、安全边界或真实故障为触发，已完成的审计脱敏不重开。 |
| C-11 | 其他非关键观测入口的异常与阻塞边界。 | LLM 调用日志已由 [LLM-049](../issues/integration/llm/2026-09-12-llm-observation-overrides-terminal.md) 实施有界隔离；其他日志、指标和审计入口若进入终态路径，仍须逐入口核验 G0-6，不能把局部实现宣称为全仓完成。 |
| C-13 | `ContextManager.build_messages` 的历史查询取「最早 N 条」而非最近的 N 轮。 | R6 复核 [CHAT-001](../issues/application/chat/2026-09-16-current-message-duplicated.md) 时确认：`SessionManager.get_messages` 用 `order_by(created_at.asc())` + `limit`，会话超过 `max_rounds * 2` 条（默认 40）后送给模型的是**最早**的历史，而 `build_messages` docstring 写的是「保留最近的 N 轮对话」。两者只有一个是对的：先确认产品意图（锚定最早对话，还是保留最近上下文），再决定改查询还是改文档。不属于 CHAT-001 的当前消息边界问题，R6 未改动该分页行为。 |
