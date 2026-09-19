# 工具执行生命周期：终止控制、事实接管与资源冲突保护

> **ID**：TOOLS-ADR-008
> **日期**：2026-09-13
> **决策状态**：六项方案选择已接受并回填；接口类型、配置值、迁移及部署机制由 P0 冻结，不重开已定方向。
> **实现状态**：部分兑现。Piece①～④完成进程内基础、事实端口、准入及真实执行接管；领域完整批次提交和持久保护仍未完成。实际测试与后续范围见[当前计划](../../../docs/todo.md#c-02-implementation-pieces)及[对齐表](../../../docs/ALIGNMENT.md)。
> **范围**：Domain 工具编排 → ToolGateway → Integration 工具执行及清理 → 持久事实 → Domain 协议历史与终态。
> **主归属**：Integration/tools；跨层协作以领域端口及装配根注入实现。
> **计划**：[C-02 生命周期实施计划](../../../docs/todo.md#c-02-lifecycle)

## Context

### 产品问题与约束

C-02 原为“工具退避对业务取消信号的响应”。核验发现问题同时涉及排队、真实 attempt、线程/进程清理、并行结果接管及未知副作用后的再次准入。只替换退避 sleep 不能兑现完整取消契约。

产品价值是 RCA 排查、文件报告和分析脚本执行中的事实可追溯、执行可终止，以及失败不拖垮无关操作。遵守 [G0](../../../docs/engineering/ai-engineering-rules.md#g0)、[运行时规范](../../../docs/engineering/agent-runtime-rules.md)及 [ADR-003](../../2026-09-12-sdk-call-guard-response-commit.md)。本决定不授权建设通用工作流引擎、自动补偿平台或全局工具熔断系统。

六项定案：分级持久化；进程内与跨重启能力分批验收；保留 code_exec 并明确限制与演进路径；attempt 级优雅取消加安全边界协作检查；独立事实收集＋选择性异常传播；首期支持单活动执行进程中的多 Agent、多批次并发，共享资源统一准入，取消与事实按所属运行隔离。

本轮替代早期讨论中的“所有调用执行意图强制落库”和“进程内交付必须等待完整账本”要求；未采用“所有业务终止统一正常返回回执”。“单次未知结果默认全工具隔离、人工解锁”也已撤回，操作事实、资源冲突与工具健康分别处理。以上为设计演进，不是已实现代码的回滚。

### 2026-09-13 静态代码基线

| 位置 | 核验事实 | 实施差距 |
| --- | --- | --- |
| [ToolGateway](../../../app/domain/ports/tool_gateway.py) | execute 未接收 cancel_event/deadline；ToolResult 主要为 success/content/error/metadata | 不能明确表达执行终止、未知副作用和清理接管 |
| [ToolService](../../../app/integration/tools/tool_service.py) | execute 先等待外部工具刷新；shutdown 遍历 on_unload | 刷新前后、卸载和在途调用需要纳入生命周期 |
| [ToolExecutor](../../../app/integration/tools/executor.py) | 信号量包裹完整调用；按工具名锁；wait_for 执行；退避用 sleep；成功处理与执行处于同一重试 try 内 | 后处理异常可能进入执行重试；本地 await 结束不能证明线程已结束 |
| [Hooks](../../../app/integration/tools/hooks.py) | 支持同步/异步回调；捕获普通异常；无统一等待上限 | 需核实实际回调性质；不能把所有 Hook 当成无副作用观测 |
| [ReAct](../../../app/domain/reasoning/react.py) | execute_tool_calls 等 gather 完整返回后才追加记录，随后生成 tool 消息 | 中断前已完成兄弟调用需要独立接管；事实与展示次序分离 |
| [文件工具](../../../app/integration/tools/builtin/file_ops.py) | writeFile 以 w 打开文件；存在异步线程支持的 I/O | 失败可能已截断或部分写入；工具名锁不覆盖跨工具同一文件 |
| [HTTP 工具](../../../app/integration/tools/external/http_api.py) | 通用方法/URL，网络异常返回普通失败 | 仅凭错误或方法不能确定副作用和重试安全 |
| [装配根](../../../app/container.py) | 进程内单例；异步 SQLAlchemy session 工厂；工具在 DB 关闭前 shutdown | 有装配位置，没有工具账本及恢复组件 |
| [TaskService](../../../app/application/task/task_service.py) | 内存 Semaphore 和取消事件 | 不是持久调度器，不具备重启恢复能力 |

以上为源代码检查，不是故障复现或测试通过声明。实现前按计划补红测及独立根因 Issue。

### 部署与持久化证据

[产品文档](../../../docs/project/product.md)描述本地单用户、固定工具场景；[main.py](../../../app/main.py)及[部署说明](../../../docs/project/deployment.md)提供本地 reload 启动，没有生产 workers/replicas 或共享挂载契约。不能据此保证部署永远单进程。

现有 ORM Base、Session/Message 与 session 工厂可复用；`app/infrastructure/database.py`、`models/database/task.py`、`models/database/tool_log.py` 等仍为空文件，不是现成工具账本。此次未发现工具执行恢复扫描、持久后台调度或通用事务认领组件，也未连接数据库或验证迁移。工具执行账本、建表/迁移、启动核验是本项真实新增工作，不能用现有审计日志冒充。

首期支持同一受保护执行范围内单活动执行进程中的多 Agent、多批次并发；单进程不等于单 Agent。普通只读工具先验收进程内闭环；需要持久保护的工具须在账本、启动恢复及相关资源保护完成后按新契约启用，重启先恢复准入保护。部署约束及必要启动排他须验证，不能仅靠进程内单例。多实例共享资源、共享文件系统保护不在首期保证内；实际需要时先补跨实例原子准入及资源身份方案。

### 工业级参照与取舍

以下为 2026-09-13 查询的官方文档/仓库；主分支资料会变化，实施时核实实际采用版本。事实与本项目推论分列，不把框架能力当成本项目已有实现。

| 官方资料 | 已核实机制 | 本项目采用与不照搬部分 |
| --- | --- | --- |
| [OpenAI Agents 工具超时](https://openai.github.io/openai-agents-python/tools/#function-tool-timeouts) | 异步函数工具超时可转模型可见错误，或配置抛异常 | 区分单次失败与运行终止；不从错误直接推出工具整体失效 |
| [OpenAI 工具生命周期](https://github.com/openai/openai-agents-python/blob/main/.agents/references/tool-execution-lifecycle.md) | 工具执行、并行已完成结果保留和取消清理有独立边界 | 逐调用接管结果；不宣称该框架替应用解决远端持久幂等 |
| [OpenAI 执行实现](https://github.com/openai/openai-agents-python/blob/main/src/agents/run_internal/tool_execution.py)、[LangGraph ToolNode](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/tool_node.py) | 前者独立收集结果并协调失败收尾；后者按配置将匹配错误转为工具消息，图控制异常继续传播 | 采用独立事实收集和选择性异常传播，不机械照搬所有错误结果化或默认整组取消 |
| [Temporal 幂等与持久执行](https://temporal.io/blog/idempotency-and-durable-execution) | 超时后旧尝试可能仍运行；重复尝试需要幂等设计，非幂等活动可限制一次尝试 | 重试按操作安全性许可，不复制其默认重试策略 |
| [AWS 幂等 API](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) | 调用方请求 ID 表达意图；相同参数可能是合法的两次业务请求 | 分离 call ID 与业务幂等键；不采用参数哈希通用去重 |
| [AWS Java 重试策略](https://docs.aws.amazon.com/sdk-for-java/latest/developer-guide/retry-strategy.html) | Standard 的高失败保护可关闭重试；Adaptive 客户端共享会扩大资源故障影响 | 健康保护匹配真实共享范围；不引入一次未知写即全工具封禁 |
| [MCP 工具协议](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) | 工具业务失败可通过 isError 表达；行为注解须考虑可信来源 | risk/readOnly/idempotent 注解不是执行安全证明 |
| [AnyIO 线程](https://anyio.readthedocs.io/en/stable/threads.html)、[取消与清理](https://anyio.readthedocs.io/en/stable/cancellation.html) | 放弃等待不停止线程；清理需单独考虑取消保护及界限 | 保留真实执行所有权；不直接套用可丢弃结果的线程等待模式 |

真实备选：A 保持粗粒度工具隔离，简单但误伤所有操作；B 只返回失败并放开执行，简单但重复副作用及并发冲突失控；C 操作记录＋必要资源保护＋独立健康判断。采用 C。资源保护的必要语义进入首期，通用分布式实现留待需求触发。

## Decision

### D1 分层职责与最小结构

| 层/组件 | 拥有职责 | 不承担职责 |
| --- | --- | --- |
| Domain / ToolGateway | 调用身份、执行控制输入、可消费的调用事实/终止契约；ReAct 协议历史与唯一终态 | SDK、数据库、工具进程清理实现 |
| ToolService / Executor | 每次真实 attempt 准入、排队、重试、执行结果接管、清理与移交 | 用户 SSE 终态、领域成本预算重建 |
| BaseTool / 具体适配器 | 真实执行、资源范围及重试安全声明、可得副作用事实、可选只读核验 | 根据任意异常猜测远端未执行 |
| 专用事实存储端口/适配器 | 操作及尝试状态、幂等更新、未决事实读取 | 重新执行工具、恢复原 Agent |
| ToolService 生命周期所有者 | 有界接管队列、回收、启动核验、关闭顺序 | 无限后台任务、通用工作流调度 |
| Infrastructure / Container | 数据库实现与事务、迁移接入、配置及生命周期装配 | 反向依赖 Domain 编排内部 |

沿现有 Facade/Executor 提取需要独立测试的控制和接管职责；不为表格每一行建类。领域端口仅含标准库类型；Infrastructure 实现存储，经 Container 注入 Integration，禁止 Integration 直接依赖具体 ORM。工具控制不得 import LLM 私有 execution_control；若确有机械等待代码可复用，先按依赖规则选择现有公共位置，不复制 LLM 业务信号。

### D2 身份、事实与控制分离

下表为必需语义，不预先冻结 Python 类名或字段布局；P0 将其映射为最小端口契约并评审。

| 维度 | 语义 |
| --- | --- |
| workflow / run / batch / tool_call_id | 协作任务、Agent 运行、工具批次和模型协议关联；结果按所属调用隔离，不得作为跨业务的天然去重键 |
| operation_id | 同一次逻辑操作的稳定身份；重试和事实写入复用 |
| attempt_id | 每次真实 BaseTool.execute 唯一，区分多次可能发生的副作用 |
| business idempotency key | 由可信业务适配器提供，作用域和参数一致性明确；不得由通用参数哈希冒充 |
| 执行结果 | 未执行、成功、失败；已开始但尚未取得结局单独表达 |
| 副作用事实 | 确认无副作用、确认完成、部分完成、未知；错误返回不自动确定它 |
| 清理/移交 | 无待清理、清理中、已清理、已移交；与业务成败正交 |
| 终止原因 | 取消、绝对期限、普通执行失败等，不覆盖已取得的执行事实 |

保持现有 ToolResult 的成功内容和业务 metadata 可消费；不能只增加任意 metadata 字符串让领域猜控制信号。默认缺少安全声明视为未知，不推定无副作用。持久状态更新须有版本/条件更新或等效原子保证，迟到旧事件不能覆盖更强的新事实，重复回收不产生第二次逻辑结算。

### D3 调用前准入与尝试预算

1. Facade 在刷新等等待之前检查控制信号；受控等待返回后复查。注册实例与版本在本次调用期间固定，热重载不得使在途操作失去所属实例。
2. 解析、校验、审批及资源描述生成先完成；它们的等待受取消/期限约束。审批通过不豁免之后的准入。
3. 统一协调执行容量与资源准入；资源阻塞的请求不能长期占着真实执行槽。多资源按稳定顺序获取或原子整组取得，避免循环等待。失败释放已取得部分；排队取消与取得许可同时完成时必须接管再归还。
4. 对 D8 要求持久化的调用记录执行意图及 attempt 身份，失败则该真实调用为零；普通只读且无强制审计要求的调用不强制此步骤。所有调用在真实调度前再次检查取消、deadline、准入及尝试额度，包括持久化等待之后。
5. 调度前发生终止：接管未执行事实，按持久化等级记录并释放许可。持久意图与外部执行之间不存在分布式原子提交；崩溃留下的意图只能解释为待核验，不能证明已执行或未执行。

真实调用消耗 attempt，等待与事实写入不消耗工具 attempt。现有 `max_retries` 实际是最大执行次数且 0 按一次处理；迁移保持其外部计数口径，不顺手改为 N+1，但加入安全许可：未明确证明该错误下可重复执行的工具最多一次。每次重试重新准入，退避可中断，总绝对 deadline 不重置；SDK 内部重试关闭或纳入可证明上界。

不可重试：参数/审批拒绝、业务终止、已完成后的记录/日志异常、缺少幂等保障的未知副作用。只读工具也不能无限重试；永久业务错误不因只读就变成可重试。

### D4 取消、期限及公开出口

业务取消的优雅单元是已经开始的一次 BaseTool.execute，不是完整重试链，不承诺事务回滚。取消请求后停止新的 attempt、排队和退避；在途 attempt 可在原有局部超时与本次总期限内返回，不另给完整新执行窗口。自有工具在尚未开始写入且可安全放弃等明确边界增加协作取消检查；已进入不能安全拆开的操作时完成必要处理并保留事实。检查点由适配器按业务语义确定；第三方工具不支持协作检查时明确以整个 attempt 为边界，不假定其内部已受控。

局部 timeout 沿 TOOLS-ADR-004 的调用方＞工具＞全局优先级解析，但真实 attempt 有效上限还受剩余绝对 deadline 限制。排队不消耗局部执行 timeout，却消耗总期限；期限已耗尽时真实调用为零。

Domain 继续拥有既有 Guard 优先级，参见 [_common](../../../docs/domain_doc/reasoning_doc/_common.md)。Integration 只处理传入的执行控制和自己拥有的容量/尝试约束，不重建 LLM token、成本或上下文预算。工具收费元数据若可得应保留，但本期不承诺工具费用的精确预留。

公开出口采用独立事实收集＋选择性异常传播：正常成功和普通工具失败返回结果；运行级业务取消/总期限通过领域可识别的类型化异常传播。事实在异常之前独立接管，异常可携必要引用，但不是唯一载体，也不强制包装整个批次结果。领域层读取已接管事实再收尾。局部工具超时不自动升级为运行总超时；多个控制原因按既有优先级判断，不依赖回执到达次序。外部 Task.cancel 保持 CancelledError 传播，不伪装业务失败；事实接管/移交不能依赖调用方继续等待。具体异常名称及类型由 P0 确定，不复用 LLM 专属异常名。

| 竞态 | 必须处理的事实 | 对外行为 |
| --- | --- | --- |
| 取消/期限先于真实调用 | 尚未执行，归还许可 | 类型化终止，无新业务调用 |
| 结果与终止同时到达 | 先接管结果、清理责任和可得元数据 | 按既有优先级选终态；成功事实仍保留 |
| 局部超时先到，总期限未到 | 旧尝试可能仍运行 | 收尾/接管后判断安全重试；不能只按 TimeoutError 分类 |
| deadline 到达，执行忽略取消 | 保留真实任务所有权 | 有界等待后移交，不无限阻塞 Agent |
| 已取得成功，审计/日志失败 | 成功不可改成需要重新执行 | 有界观测隔离；关键记录失败单独处理 |
| 外层硬取消与迟回值并发 | 迟回值的事实/资源不丢弃 | 原取消传播，Integration 接管剩余责任 |

Domain 透传同一内部业务 deadline，外层 timeout 作为最终取消触发器，沿现有 ReAct 提前清理窗口规则，不重新累计宽限。不得声称 asyncio timeout 能物理终止任意阻塞代码；同步阻塞不能放在事件循环上。清理窗口只允许清理及事实移交，不允许付费总结、补偿写入或业务重试。

### D5 调用后事实与并行提交

真实调用一旦返回，先取得原始结果所有权，做最小必要解码，保留成果、来源 metadata、失败阶段及部分副作用；再结算/移交执行资源，完成必要事实更新。展示截断不覆盖恢复所需依据，敏感信息按现有安全契约限制存储与展示。

成功后的截断、统计、Hook、审计不进入工具执行重试 catch。现有 Hook 必须盘点：普通观测有界且失败不覆盖结果；必要事实记录按关键路径处理；会新增业务副作用的 Hook 不能藏在“观测”中，须移至受准入的明确业务步骤。

执行成功但必需的完成/证据记录写入失败时，保留确认成功、关闭本次运行后续业务准入，仅重试幂等记录；所属运行的兄弟在途调用按原上限收尾，排队调用停止。非关键观测写入失败不采用此规则；其他运行仅在自身必需存储或共享资源准入不满足时受限。进程恰在必需记录成功前崩溃，可能只能恢复为未知，这是跨系统原子性限制，不能保证所有迟回结果必然可恢复。

并行任务逐个形成可接管回执，完成一个接管一个。Domain 不等待整个 gather 成功后才承认事实；部分失败或取消时也可读取已经接管结果。事实到达顺序与协议展示顺序分离：统一由主生成器发布事件，保持本项目稳定输入顺序及 call ID 配对，不在并发 task 内 yield SSE。

批次收集器按调用身份保存结果与控制原因，收到单工具控制异常后先接管事实并协调所属兄弟调用，再按已定控制语义向上传播，不将控制异常转为可继续的普通错误。业务优雅取消不能机械 cancel 所有在途兄弟；外部硬取消仍及时传播并由 Integration 接管未完责任。各调用自行观察共享控制信号，不能等待最慢任务先抛异常才通知其他调用。

提交协议历史前，对每个 assistant tool_call 生成真实成功、失败、未执行或结果未知的 tool 回执；禁止编造成功，禁止发送缺失配对的已知非法 history。终止时不再为修补历史调用 LLM。原始事实、模型可见历史和最终成果采用明确映射，不能把未知写入当成可靠证据。

Agent 唯一终态由 Domain 提交。迟到事实更新操作记录，不再次发 done、不恢复旧 Agent、不自动生成第二份答案。消费者提前关闭异步生成器时，同样保留接管责任。

### D6 按资源保护，不按一次失败封禁工具

普通 404、输入错误、确定业务失败属于本次调用。未知写入需要记录操作并阻止不安全的重复/冲突操作；独立资源仍可使用。仅有明确共享客户端、凭证或环境故障证据时，才扩大到对应健康范围。本期不新增通用熔断器。

资源描述来自可信适配器/配置，不能由 LLM 任意声明以绕过保护。至少表达所属存储/环境范围、资源身份和访问方式；一次调用可涉及多资源。资源级冲突判断与 `concurrency_safe=False` 的实例互斥并存，后者不是副作用安全声明。

| 工具族 | 首期资源与恢复规则 | 不承诺 |
| --- | --- | --- |
| RCA 模拟只读工具、网页读取 | 不因对象失败产生持久写保护；按错误有界重试 | 网站失败不代表浏览工具损坏 |
| search / 线程 SDK | 在途线程直到真实结束前保留容量；可得结果由 Owner 接管 | cancel await 不等于线程停止 |
| readFile / writeFile | 存储范围＋规范文件身份；同文件跨工具冲突受控；核验读取另行许可 | 原始路径字符串、仅 normcase 或工具名称不是完整身份保证 |
| HTTP | 专用适配器提供业务资源与幂等契约；通用未知写采用可信配置的较粗目标范围 | HTTP 方法、URL 或相同参数不证明业务重复安全 |
| code_exec | 受控工作区/执行环境范围；与文件工具使用同一范围层级判断，范围级写与子资源写冲突 | 不能从任意命令精确解析所有资源，也不保证 kill shell 已停止后代和远端副作用 |

范围级保护必须覆盖子资源，不能只比较相同字符串。文件别名、链接、硬链接及目录操作要在 P0/P3 定义可保证集合；无法可靠识别时扩大到受控目录/环境或拒绝该能力，不假称精确互斥。任意代码若可越出声明环境，必须限制执行边界或标为不具备该安全保证，不靠 metadata 声明解决。

一个未知操作不会封禁所有用户的同名工具。反之，资源确实共享时不能仅加 tenant/run 前缀让同一物理文件绕过保护。没有可靠细粒度身份时使用已声明的保守范围；没有任何可保证范围的副作用能力不得以“已具备完整生命周期保护”启用。

### D7 所有权、容量与恢复

| 对象 | 取得者 → 正常完成者 | 中断后责任 |
| --- | --- | --- |
| 排队许可/部分资源准入 | Executor → Executor 归还 | 取得与取消竞态仍归还一次 |
| 真实 task/thread/process | Executor/适配器 → 真实结束后释放容量 | 转交 ToolService 生命周期 Owner；不能仅 await 结束就退容量 |
| 结果/失败事实 | 适配器 → Executor 接管 → 持久记录 → Domain 消费 | 持久记录或待写回执保持唯一责任 |
| 未知冲突限制 | 准入 Owner → 核验证据满足后解除 | 跨重启读取恢复；不得因对象析构丢失 |

接管数量、清理时间、记录重试和核验次数都必须有配置上限；后台队列满则停止相关新准入，不能丢弃已执行事实腾位置。配置由 Container 注入；P0 根据现有并发与工具上限确定默认值，唯一维护在配置参考中，不在 ADR 发明固定秒数。

区分三种完成：执行容量释放、冲突消除、业务结果确认。线程结束可释放容量，但未必确认文件内容；远端请求仍可能执行时，本地 task 结束也不解除远端写冲突。确认旧执行不能再影响资源后，可以解除物理冲突而保留业务未知记录；依赖完整结果的业务仍须核验。

自动核验仅使用适配器提供的确定性只读查询，有次数/时间上限；不能由 LLM 临时编造查询替代事实，不能自动补偿或重复原写入。人工恢复记录操作者、证据、受影响操作/资源及允许的恢复动作；不是无条件“解锁工具”。时间到期、租约失效不是远端停止的证据。

卸载/热重载先停止该实例新准入，已有执行和接管记录持有旧实例/版本直至完成；不得关闭仍被使用的共享客户端。正常关闭顺序为停止新准入→有界清理及事实移交/刷写→确认相关 Owner 已释放依赖→关闭工具客户端→关闭持久存储。

关闭窗口耗尽仍有真实执行时，不得继续把“关闭正在使用的客户端/数据库”当成成功的优雅关闭。记录关闭未完成及可恢复未决事实，停止新准入，按部署的强制退出边界交由宿主终止进程；进程仍存活期间保持未结束 Owner 必需依赖可用。首期 P0 必须确定并验证宿主退出方式，否则只能保证 Agent 收尾有界，不能宣称应用进程关闭有界。强制退出可能丢失未落库迟回结果；恢复时按未知核验，不能凭已有意图伪造结果。进程终止也不证明远端操作已经停止。

### D8 持久化与重启边界

采用分级持久化。可能产生副作用、分类不明或要求强制调用审计的操作，真实执行前必须可靠记录意图；确认无副作用且无强制调用审计要求的纯计算、模拟读取、普通只读查询不统一要求调用前落库。分类由可信适配器/宿主配置确定，不能仅凭工具名称、HTTP 方法、风险等级或模型声明。缺少可靠分类时按可能有副作用处理。

调用前意图服务于恢复/重放安全；调用后结果与证据保存服务于成果和审计，两者独立。只读 RCA 的来源、量测值和时间等仍按证据契约可靠接管，必要保存完成后才用于业务提交。免除意图落库不免除事实接管，也不意味其可绕过其他运行持有的共享资源保护。

首期第二批沿 PostgreSQL/SQLAlchemy 基础实现必需的专用账本，不新增 Redis 队列或兼容存储。既有结构化审计保留观测用途，不能冒充可恢复事实数据库。存储不可用时需要账本的工具真实执行为零，不统一阻断不依赖该存储且准入满足的只读调用；此前已确认成功不能变成可重试失败。

记录至少关联身份、工具版本、资源范围、执行阶段、事实确定性、回执/必要结果、待清理责任及状态版本。最小化敏感参数，仅存恢复所必需且经授权的数据；不能为了核验把凭证写入账本。删除/保留策略不得清掉尚承担恢复或冲突责任的记录。

启用持久保护后，启动先验证 schema/存储与单活动 Owner 边界，再读取未决操作恢复准入保护，最后开放相关工具；无法恢复时禁止可能绕过该保护的资源访问，包括相关读取，不按旧意图自动重放。无关且不需要该存储的只读能力按独立准入判断。monotonic deadline 只在当前进程用于执行控制，不跨重启或主机复用；持久时间用于审计/恢复期限，重启不能给旧 Agent 新预算。

本地账本事务不能原子提交远端副作用。提供的是可追溯事实、有限重试与冲突约束，不是通用 exactly-once。数据库不可用与未知写结果是不同原因：前者可能使当前运行无法继续记录，后者只限制相关业务；不得统一归为工具损坏。

### D9 重放防线与剩余限制

Executor 内重试、上层模型再次调用、重启恢复是三个入口，都要通过操作/资源准入。工具 call ID 改变不能绕过未决资源保护；有业务幂等键时复用同一意图身份。缺少业务键时，系统不承诺识别任意语义等价请求，只能保护已声明冲突范围，无法安全放行的相关写入需核验或人工裁决。

健康检查、只读核验和有界回收可在旧 Agent 终态后继续，因为它们承担已经发生事实的恢复责任；新的业务尝试、补偿、付费总结不属于该例外。

### D10 多 Agent、多批次的共享执行与运行隔离

首期单活动执行进程中复用共享 ToolService/Executor；不新建完整多 Agent 编排平台。Application/Domain 拥有工作流和 Agent 编排，Integration 拥有真实工具执行管理。每次运行具有独立取消、结果和批次收集状态，不并发复用持有可变成果的策略实例。

| 范围 | 责任 |
| --- | --- |
| 协作任务 | 父取消/总 deadline；子 Agent 失败是否终止其他 Agent 由编排策略决定 |
| Agent run | 自身取消、预算与成果；父取消向下生效，子取消默认不横向传播 |
| batch / call / attempt | 独立事实与协议身份、局部超时、有限重试、清理责任 |
| 共享执行范围 | 全局真实执行容量、单运行在途上限、有界等待队列、跨 Agent 资源冲突及接管容量 |

各子调用采用所有适用绝对 deadline 的最早值，局部 timeout 在 attempt 开始时进一步收紧；父层已有清理窗口不逐层重复扣减。一个 Agent 取消不取消无关 Agent，但其仍在运行或未知的写入可能继续阻止其他 Agent 的冲突调用。共享资源键不因 run/tenant 不同而分裂，事实归属键则必须区分运行。

首期采用简单按运行轮转的有界准入，选取资源条件满足的请求；资源阻塞不占真实执行槽，保留等待次序防止旧请求持续被插队，不能把公平性寄托于多把独立信号量。全局容量不是各 Agent 配额的简单相加。执行转入后台接管不退真实容量；本地执行结束但远端未知时，本地容量与业务冲突限制分别管理。共享状态由所属事件循环更新，工作线程不得直接操作 asyncio 同步原语，见 [Python 同步原语](https://docs.python.org/3/library/asyncio-sync.html)。

编排父任务不占子工具需要的真实执行槽。首期不支持普通 BaseTool.execute 任意递归进入同一工具网关；Agent-as-tool 需要明确编排边界后再支持，避免持有全部许可等待子调用的死锁。异常或单个 Agent 结束不销毁共享客户端，应用生命周期 Owner 负责正常/强制关闭边界。

### D11 code_exec 具体限制与未来可行方案

保留代码执行能力，默认视为可能有副作用，满足意图持久化后才按新契约启用；未证明重试安全时一次尝试。保留退出状态、部分输出和未知事实，使用实际可证明的资源范围；工作目录不等于沙箱。范围不明确时，暂停对应代码执行环境的新调用并保留核验通路，不宣称已阻止所有范围外影响。普通命令失败且执行已确认结束不自动要求人工解锁；已知部分副作用仍按相应事实处理。

| 当前限制 | 未来可行方案 | 启用条件及剩余限制 |
| --- | --- | --- |
| 工作目录无法约束任意文件访问 | 独立沙箱、挂载白名单、最小权限 | 需要不可信代码或资源隔离时建设；不能只增加工作区 metadata |
| kill Shell 不证明后代全部停止 | 平台进程组、Windows Job Object 或容器生命周期管理 | 需要本地强终止保证时验证整个进程树；仍不撤销已发生写入 |
| 本地退出不证明远端未执行 | 网络出口限制；具备幂等键、状态查询的业务适配器 | 引入真实外部写入时按 API 契约设计；沙箱本身不解决远端幂等 |
| 任意命令难以准确推导资源范围 | 受约束执行环境、结构化操作入口 | 需要细粒度冲突控制时减少任意命令自由度，不用参数推测冒充证明 |
| 崩溃可能丢失未落库输出 | 独立执行进程、持久输出通道及确认/恢复协议 | 输出恢复成为实际需求时建设；仍须明确执行与记录间的未知窗口 |

上述为可行性方向，具体平台、接口与验证方案在需求触发后另行冻结；首期不为消除全部限制默认建设沙箱平台。

### D12 分批交付及升级路径

第一批完成进程内取消/期限、共享准入、安全重试、独立事实、选择性异常及有界清理，用普通只读工具验收；不承诺崩溃恢复。第二批完成分级账本、必要资源保护、启动恢复及核验后，逐个启用满足条件的副作用工具。开发批次可拆，工具启用条件不能降低；独立缺陷修复不等于完整生命周期验收。

| 触发条件 | 升级方向 | 边界 |
| --- | --- | --- |
| 多租户/明显争抢 | 租户配额、加权公平调度、优先级和等待指标 | 在首期简单公平准入之上升级，不预建复杂调度器 |
| CPU 密集/不可协作取消 | 专用执行进程、进程树管理 | 与 D11 协同，不保证撤销远端副作用 |
| 多 worker 共享资源 | 原子准入、可信资源身份、Owner 协调及必要 fencing | TTL 过期不证明旧执行停止，不能仅把内存锁换成 Redis 锁 |
| 跨机器工具执行 | 独立执行服务、持久命令/回执、幂等消费 | 网络断连与重复交付必须有恢复契约 |
| 长时执行/跨重启继续编排 | 评估 Temporal 等持久工作流 | 记录事实不自动等于恢复 Agent；执行取消仍需协作，见 [Temporal](https://docs.temporal.io/develop/python/workflows/cancellation) |
| 持续依赖故障 | 按真实共享范围限流、熔断、恢复探测 | 不以单对象失败触发全工具封禁 |

<a id="tool-lifecycle-p0-spec"></a>

## P0 实施规格（2026-09-13；Piece ①、②已实现）

本节冻结实施方案。Piece ①、②已创建调用边界、运行身份与事实接线；后续 Piece 的类名/路径仍是计划目标，不冒充现有组件。配置唯一见[配置规格](../../../docs/config_doc/config.md#tool-lifecycle-p0)，部署机制唯一见[部署规格](../../../docs/project/deployment.md#tool-lifecycle-p0)。已实现状态和验证映射以 [ALIGNMENT](../../../docs/ALIGNMENT.md) 与 [todo](../../../docs/todo.md#c-02-implementation-pieces) 为准。

### S1 公开接口及事实所有权

继续由 `ToolGateway.execute` 返回 `ToolResult`；保留 name、parameters、timeout、max_retries、retry_delay 现有位置与计数口径，新增必填仅关键字参数 `call: ToolCallContext`、`facts: ToolFactSink`。所有实际调用方、脚本及测试替身同批迁移；不为旧签名静默生成无归属全局运行。直接调用者可显式创建独立 run/batch 和收集器。不会把 lifecycle 参数混入模型 JSON Schema。

`app/domain/ports/tool_execution.py` 新建且仅放领域拥有的工具执行值类型与事实端口；不放 SDK、存储或任务调度逻辑。保持 ToolResult 在 tool_gateway.py，不搬迁已有导出以制造重构。

| 类型/成员 | 字段或方法与契约 |
| --- | --- |
| ToolCallContext（冻结值对象） | workflow_id 可空；run_id、batch_id、tool_call_id、operation_id 非空；deadline 和 cleanup_deadline 可空且为本进程 monotonic，后者由领域外层取消触发点派生，只供清理；cancel_events 为父/本运行 Event 元组；run_stop 为同运行共享 Event，只关闭该运行新业务准入，不代替取消原因 |
| ToolFact（冻结快照） | operation_id 非空；attempt_id 仅在未创建 attempt 时可空；run_id、batch_id、tool_call_id 为当前消费者身份；revision 属于所引用操作/attempt 的事实版本；execution_state、effect_state、cleanup_state；result 可空；diagnostic_id 可空。结果及 metadata 复制为该快照专有数据，不暴露可被工具后处理改写的引用 |
| execution_state | NOT_STARTED / RUNNING / SUCCEEDED / FAILED / UNKNOWN；RUNNING 表示确认仍在执行，UNKNOWN 表示无法确认执行结局，不伪装为未执行或确定仍在运行；真实容量仍依据底层句柄，而非仅凭此枚举释放 |
| effect_state | NONE / COMPLETED / PARTIAL / UNKNOWN；ToolResult 新增同型 effect_state，缺省 UNKNOWN；可信只读适配器可确认 NONE，success=False 不自动决定效果 |
| cleanup_state | NOT_NEEDED / PENDING / COMPLETE / TRANSFERRED；TRANSFERRED 只是调用者已移交，不代表底层结束 |
| ToolFactSink.record(fact) | 同步、无 I/O、同事件循环记录；按 operation/attempt/revision 幂等更新。不能 await，不调用任意用户 Hook；异常是编程错误，Integration 保留自身事实并关闭所属 run 准入后向上报告 |

Integration 是执行事实的第一 Owner，先保存再通知 sink；Domain 的 `ToolBatchCollector` 是每批次可见快照 Owner，事实不只存在异常里。批次结束或消费者关闭后 collector 停止接收更新，Integration 解除其引用；晚到事实留在接管记录/必要账本，不延长整个 Agent 对象寿命。任何正常返回或类型化控制抛出前，调用者至少收到终局或 TRANSFERRED 快照；硬 Task.cancel 不保证该交付，但不能丢 Integration 已取得事实。

业务键命中时，ToolFact.operation_id/attempt_id 引用原规范操作，run/batch/call 仍指向本次消费者；ToolCallContext.operation_id 是本次拟登记身份，命中后不能据此伪造第二次执行。collector 按当前 call 与规范 operation/attempt/revision 去重，不把两个消费调用合并成一条协议消息。首期对未决原操作只交付当前未决快照及明确的未完成工具结果，不等待或订阅其未来完成，也不重放；迟到事实继续由原操作 Owner/账本接管。已完成原操作只复用允许对当前调用者披露的结果。

ToolResult 的 effect_state 枚举由 tool_execution.py 定义；ToolFact 对 ToolResult 使用延迟类型注解和 TYPE_CHECKING 导入，避免两模块运行时循环导入。持久化显式转换快照，不依赖运行时自动解析该前向引用。

共享异常唯一放 `app/shared/exceptions.py`：`ToolCancelledError`、`ToolDeadlineExceededError`、`ToolRunStoppedError` 继承 NonRetryableError，分别新增 TOOL_CANCELLED、TOOL_DEADLINE、TOOL_RUN_STOPPED 业务码，只携 run_id/operation_id/诊断引用，不依赖 Domain 类型。第三种表示关键事实/恢复前提失败关闭 run，Domain 保存成果后 STOP，不交 TOOL_FAILED 的 CONTINUE 路径。局部超时仍为 ToolResult(ErrorCode.TIMEOUT)，不抛 ToolDeadlineExceededError。非预期编程错误记录诊断后继续传播，不能转为安全重试。

共享控制检测顺序：取消（任一 cancel_events）→本调用绝对 deadline→run_stop。Domain 成本/context 继续使用既有 evaluate_guard；这不是新增一套 LLM 优先级。父级取消事件通过上下文引用传入，不把多个运行绑定到一个全局 Event。

### S2 适配器边界与重试安全

BaseTool 保留 execute(**parameters) 作为业务实现，新增 `invoke(parameters, execution)` 控制入口，默认委托 execute；自有需安全检查/跟踪线程的适配器覆写 invoke，执行数据和宿主控制分别传递，避免 `_context` 一类隐藏参数与业务字段碰撞。不增加只为兼容旧调用签名的探测分支。

BaseTool 新增 `describe_execution(parameters) -> ToolExecutionSpec` 与 `can_retry(result_or_error) -> bool`。Spec（Integration 内值对象）包含 effect_class=READ_ONLY/MAY_WRITE/UNKNOWN、audit_required、resource_claims 元组、tool_version；默认 UNKNOWN、不可自动重试、资源范围由可信注册配置补齐，不能默认为空范围的安全写入。方法无业务副作用；若需要探测文件身份，探测也受外层期限约束。单个适配器的错误分类与自身协议一起维护，不设通用 RetryPolicy 类。

B 为 Spec 增加可空的 intent_fingerprint/fingerprint_version 及显式业务键/范围，生成与复用条件见 S7；无比较能力时允许为空但不得复用已有身份执行。适配器可提供 `reconcile(reference, execution)` 受控只读核验入口，缺省声明不支持，不以调用原 execute 代替核验。reference 为脱敏恢复引用，返回事实证据，不能自行释放共享保护；ToolRecovery 负责次数、期限、存储与结果应用。核验准入须明确不与原未知操作产生新的危险冲突。

`execution` 是每 attempt 的 ToolAttemptHandle：提供 `check_abort()`、`run_sync(fn, ...)` 和底层完成登记。run_sync 使用宿主拥有的线程池，保存 concurrent Future 的真实完成状态，通过 loop.call_soon_threadsafe 回传；取消外层 asyncio Future 不当作线程已结束。新线程工作只能在本 attempt 准入下发起，不创建另一套无限线程队列。子进程保留 proc/communicate 的实际句柄；无法证明所有后代已停止时事实明确 UNKNOWN。

重试准许须同时满足：can_retry 对该错误返回真、实际尝试次数未耗尽、取消/期限/run_stop 未触发、前次执行已结束或有明确幂等及冲突契约。重试前释放已无责任的真实许可，退避不占执行槽；下一 attempt 重新竞争。同一调用所有 attempt 使用同 operation_id，不重置总 deadline；ToolResult.retry_count 保持实际 attempt 数，未执行为零。SDK 自带重试在适配器专项测试中关闭或核算，不能仅凭外层 for 循环宣称上界。

### S3 文件、类与方法职责（E1/E2/E5/E7/E9）

下表是最终分工而非同时新建清单。仅在所属批次创建文件并接上真实调用方，测试不通过前不发布该能力。

| 文件/阶段 | 类或代码块边界 | 新抽象依据 / 禁止混入 |
| --- | --- | --- |
| domain/ports/tool_execution.py / A | ToolCallContext、ToolFact、状态枚举、ToolFactSink | Domain/Integration 两侧真实契约；不含执行状态机 |
| domain/reasoning/tool_batch.py / A | ToolBatchCollector.record/snapshot/close；ToolBatchRunner.run | 批次具有独立任务集合及快照生命周期；runner 负责并行收集/清理协调，collector 只保存事实；不拥有共享资源或数据库 |
| integration/tools/executor.py / A/B | execute 编排准入→prepare→attempt loop→结果接管→后处理；_prepare_call、_run_attempt、_complete_call 各一个明确阶段 | 保留现有 Executor；重试计数局部，不新增 RetryManager；方法不塞入线程池/SQL/协议历史细节 |
| integration/tools/admission.py / A，B 扩充 | ToolAdmission.acquire/release/close_run/close；私有 Permit；Integration 共用 ResourceClaim | 独立共享队列/容量/资源所有权；返回幂等许可，不负责工具执行和日志 |
| integration/tools/execution.py / A | ToolExecutionSupervisor.start/wait/close；ToolAttemptHandle.check_abort/run_sync；局部受控等待 helper | 独立在途任务、底层句柄和接管生命周期；不承担重试、资源身份推导、SSE 或数据库事务 |
| integration/tools/recovery.py / B | ToolRecovery.record_intent/record_fact/reconcile/start/close | 必需事实写入与有限恢复的独立生命周期；不执行原业务重放，不做全局调度 |
| domain/ports/tool_execution_store.py / B | ToolExecutionStore.create_intent/append_fact/list_unresolved/begin_reconcile/resolve | 持久化能力端口；标准库类型，条件更新而非通用 CRUD |
| infrastructure/models/database/tool_execution.py / B | 执行账本 ORM 模型 | 与既有 Base 共用；不复用观测 tool_log 空文件承担第二职责 |
| infrastructure/tool_execution_store.py / B | SqlToolExecutionStore | 事务、SQL/CAS、错误诊断；不决定重试原业务或领域终态 |
| infrastructure/tool_owner.py / B | LocalToolOwner.acquire/close | 单机进程排他文件句柄生命周期；无 Agent/SDK 依赖 |
| shared/exceptions.py / A/B | 三个公开工具控制异常与码 | 遵守现有异常唯一来源；不 import Domain/Integration |
| scripts/migrate.py、scripts/init_db.py / B | CLI 参数解析与委托；SQL 版本文件位于 migrations/tools/ | 使用现有空脚本入口；只改工具 schema，不自动接管整个数据库迁移 |
| scripts/run_tool_host.py / B | 单 worker 宿主启动与关闭看门狗 | 部署生命周期，不放入 Executor；不建设多 Agent 调度平台 |

ToolService 只装配调用、固定工具版本及生命周期委托；validator/security/result_processor/stats/hooks 保持各自职责。非关键同步观测默认不允许阻塞式用户回调；需要阻塞 I/O 的观测须进入受控执行路径并计入宿主工作容量。不能把这些处理塞进新的万能 context/state 或 Supervisor。

ResourceClaim 是 Integration 内部供适配器描述和准入共同使用的值类型，不是 Domain 存储端口依赖的类型；Permit 才是准入内部私有句柄。ToolRecovery 将资源声明转换为存储端口定义的标准库字段快照，Infrastructure 不反向导入 Integration 类型。

代码块按“准备/取得所有权/受控执行/接管/提交”组织；try 范围只覆盖其所分类操作，清理 finally 不启动新业务；注释说明所有权与不变量，不逐句复述。文件大小仅为审查信号，不按固定行数拆函数。新增类通过 E9 的原因已逐项列出；P1 的局部异常范围修复不需要先新建以上所有文件。

### S4 实际消费方与批次执行接口

真实链路核实：ReActStrategy.execute_tool_calls 调 ToolGateway；ReActAgent._execute_tool_calls 是桥接；Reflection/Planner 通过各自 `_react.execute` 间接消费，不存在已接线的完整多 Agent 工作流调度器。chat 每次新建 Agent，但 TaskService 取消事件按 session_id 管理，需要针对同会话并发修正归属。

- `react.py` 保留领域阶段编排，将并行任务收集委托 ToolBatchRunner，历史格式化与事件提交仍在 Domain；在任何 await/yield 之前登记可得结果，再按输入顺序提交 tool 消息。
- `domain/agent/executor.py` 桥接透传本 run 的上下文；Application 入口在登记取消事件前创建唯一 run_id，并传入 Agent；base.py 校验、使用该身份，运行结束释放自身状态，不重新生成。直接调用 Agent 的入口也须显式创建并传入运行身份。同一个 Agent/Strategy 实例并发 execute 明确拒绝（本地 running 标志 try/finally），不静默互相覆盖。
- `reflection.py`、`planner.py` 的子 ReAct 共享父 run 控制与全局预算，步骤拥有不同 batch/call 身份；不为顺序子跑重新生成独立的全局预算或重复扣清理 grace。
- `application/task/task_service.py` 改为 run_id→Event，并维护 session_id→run_id 集合；`api/routes/chat.py` 登记/释放指定 run。保留现有按会话 stop 的外部语义，明确取消该会话全部活动运行；工具内部取消与 run_stop 只作用对应 run，不影响同会话其他运行。未来定向停止单 run 的 API 不在本期新增。
- ToolBatchRunner 在调用前为全部合法 call ID 预登记 NOT_STARTED；受控提交任务并立即收集已完成 facts；出错时先归并快照再传播确定性控制异常，不因一个普通失败强制取消兄弟。硬取消路径在 finally 有界收尾/移交后继续传播。

`ToolFactSink` 的同步记录不承担数据库写入；Integration 先持有本地事实再通知 Domain。批次或运行 finalizer 不靠再次查询关闭的 Gateway 猜当前结果，使用已接管快照。外部生成器提前关闭也关闭 collector 引用，不放弃 Integration 的底层所有权。

### S5 准入状态与公平性

ToolAdmission 在一个事件循环内维护各 run FIFO 队列、轮转游标、活动 Permit 和资源声明。不先为每条请求创建无界 task；达到单 run/全局排队上限时明确返回 ToolResult 的新系统码 CAPACITY_EXCEEDED（未执行），执行器不自动重试排队失败。

合法转换：QUEUED→GRANTED→RELEASED，QUEUED→WITHDRAWN；GRANTED 后取消仍由取得者释放 Permit。计数修改只在无 await 的临界段；等待者唤醒后再次检查控制。资源全部可授予且全局/单 run 容量满足时整组授予。排队中的写声明阻止后来的冲突读插队，保证已有写请求不被持续读饿死；与其无关的资源仍可被调度。

占用本地执行槽的是真实在途执行；底层未结束的移交不增加“免费后台容量”。Supervisor 在 start 前为可能移交的句柄预留跟踪条目，接管容量不能在硬取消时才发现不足。记录重试占用独立有限恢复队列；满时关闭相应新准入，保留已有条目，不丢弃事实腾位置。未知远端的冲突声明不永久占本地线程槽，但保留资源保护。

资源声明首期只支持范围层级＋READ/WRITE。跨工具同一资源读读允许，写与任何冲突读写互斥；已排队写不被新冲突读越过。核验读取走受限 recovery 入口而非普通模型参数指定 bypass，允许条件由适配器证明。没有普遍允许的“核验读不受锁影响”。

### S6 首期能力集合与资源标识

| 工具 | A / B | 效果/意图等级 | 准入、重试及核验 |
| --- | --- | --- | --- |
| 五个 RCA 模拟查询 | A | READ_ONLY；无强制意图 | 保留 source/query/timestamp；参数/业务错误不重试；无副作用核验 |
| web_browse | A | READ_ONLY（受信实现） | 网络暂态可有界重试，404/SSRF 不重试；client/stream 回收 |
| search | A，完成线程跟踪才算通过 | READ_ONLY（受信调用契约） | 真实 Future 完成才退容量；仅明确网络暂态重试；账务/强制审计要求未来变化时重分类 |
| readFile | A | READ_ONLY | 初期按允许目录范围 READ；确定缺失/权限错误不重试；启用 B 后遵守该范围未决写保护 |
| writeFile | B | MAY_WRITE；强制意图 | 初期整个允许目录范围 WRITE，跨目录/别名不确定扩大到全部受控目录；一次尝试；写前协作检查，打开/部分写后错误不宣称 NONE |
| code_exec | B | UNKNOWN；强制意图 | 已批准本地执行环境范围 WRITE，与其可访问的文件工具根范围冲突；一次尝试；无通用自动核验；D11 限制始终保留 |
| http_api | B（整个通用工具默认） | UNKNOWN；强制意图 | 可信宿主必须声明 endpoint 组/环境冲突范围，缺省未知不可作为 A 只读接口；一次尝试；无通用查询核验 |
| 未分类插件 | B 准入要求 | UNKNOWN；强制意图 | 无可信范围配置时禁止按完整保护契约执行；不从 risk_level 默认 L0 推断安全 |

首期不兑现精确文件 inode 级并发。范围 ID 由宿主固定 execution_scope＋允许根目录配置生成，路径使用规范绝对路径/大小写规则处理，重叠根合并；文件名不是互斥键。链接逃逸或跨根身份无法证明时拒绝路径或扩大保护，不以 resolve/normcase 自动保证硬链接及 TOCTOU。已存在范围外外部进程访问不受本系统锁保护。目录级策略牺牲同目录并发，换取可审查边界；未来细粒度身份按 D11/D12 升级。

人工允许 code_exec 不代表确认其副作用范围完整。它只能在 D11 限定保证下启用；未知后停止对应环境新调用，不能宣称拦住所有范围外效果。通用 HTTP 不以 GET 字符串直接降为 A；确需只读时另由可信适配器声明真实接口契约。

### S7 持久模型、事务与恢复

PostgreSQL 驱动接入及迁移作为 B 新增：当前 pyproject/uv.lock 未声明 asyncpg/alembic，scripts/init_db.py、scripts/migrate.py 为空，不能复用不存在的迁移。采用 SQLAlchemy Core 显式 SQL/事务配合版本化 SQL 迁移，不同时引入 Alembic 和第二迁移机制。

| 表（工具专属） | 最小字段与约束 |
| --- | --- |
| tool_operations | operation_id PK；workflow/run/batch/call 身份；tool_name/version；execution_scope；intent_level；claims JSONB；effect_state；resolution；revision；reconcile_attempts（初始零）；UTC 时间；诊断/结果引用；可空 business_key/scope，非空时范围内唯一；intent_fingerprint 及 fingerprint_version。unique(run_id,batch_id,tool_call_id)；不同业务参数不能复用该身份 |
| tool_attempts | attempt_id PK；operation_id FK；attempt_no；owner_instance_id；stage=INTENT/STARTED/FINISHED；execution/effect/cleanup；最小结果 JSONB；revision；UTC 时间。unique(operation_id,attempt_no) |
| tool_fact_events | event_id PK；operation/attempt 引用；revision；事实/恢复证据；actor（人工时必需）；UTC 时间。唯一事件键令写入重试幂等，不追加重复账务效果 |
| tool_schema_version | 工具迁移版本及校验和；版本不符阻止 B 启用，不影响独立满足准入的 A 能力 |

create_intent 在事务中登记操作及 attempt；append_fact 以 expected_revision 条件更新并同事务写事件。重放相同 event_id 返回原结果；版本冲突读取当前事实并只允许加强，不以最后写入覆盖。普通状态迁移与 resolve 同样走 CAS。SQLAlchemy 的 ORM version counter 不能替代任意批量 SQL 条件更新，参见[官方版本控制边界](https://docs.sqlalchemy.org/en/20/orm/versioning.html)。

“加强”依据同一 attempt 的合法阶段及证据，不按枚举数值大小排序。旧 attempt 迟回只能更新自身事实，再重新归并操作聚合；不能覆盖较新 attempt 的执行结局。业务幂等键命中另一操作时验证相同意图及参数一致性后引用原操作，不新发调用；run/batch/call 消费关联通过事实事件保存，不覆盖原操作归属。

意图指纹及规范化版本由可信适配器生成，覆盖所有影响业务效果的输入，用于同身份/显式业务键命中的一致性校验，不作为通用语义去重键。指纹不得暴露原始秘密，也不能只对可枚举的秘密做裸哈希；需要秘密参与比较时使用宿主持有的带密钥摘要及稳定密钥版本。无法提供安全、完整比较依据时拒绝已有身份的再次执行，不启用跨调用业务键复用；不得省略字段后宣称意图相同。此限制不阻止首次登记独立意图。

阶段许可：INTENT→STARTED→FINISHED；INTENT 可直接 FINISHED/NOT_STARTED；TRANSFERRED 不伪造 FINISHED。任一中间阶段重启均可能有副作用（STARTED 落库与远端执行也不原子）；恢复只核验不重放。PARTIAL/UNKNOWN 只有适配器证据或人工 resolve 可改变；本地进程消失不自动解除远端保护。

敏感数据按 allowlist 保存：不存原始 HTTP headers、凭证、完整任意命令、未脱敏参数；保存必要工具版本、目标范围、操作身份、阶段及经限制的结果。恢复查询仅使用不含秘密的业务引用，从宿主重新取得凭证。无安全恢复引用的通用命令/HTTP 默认人工核验。无自动 TTL 删除未决记录；已结束结果按配置保留期限清理，但仍有冲突/恢复责任的事件不可删除。

记录尝试有有限次数和单次/总时间预算，耗尽进入待核验状态并保留必要意图；关闭 run 新准入。自动 reconcile 每条未决记录至多执行配置次数的确定性只读查询，持久保存计数，重启不清零；不支持 query 的直接进入人工处理。人工 resolve 为专用受信命令/运维入口，要求 operator、证据、明确动作；不新增管理 UI，也不能将 UNKNOWN 一键改成未执行。

恢复扫描分页且不一次把全部记录装进内存。未决数量超过恢复容量时，相关执行范围保持关闭，分批核验消减；不得截取前若干记录后开放可能冲突的调用。独立 A 能力只有证明不访问受保护范围时可继续。自动核验尝试发起前持久消耗计数，核验期间崩溃仍计一次，保证反复重启不形成无限查询。

begin_reconcile 在单个事务中以 expected_revision、未决状态及累计上限为条件递增 reconcile_attempts、更新版本并记录唯一核验事件；重复事件不重复扣次数。只有事务确认成功才启动查询，提交结果不明时先按事件身份核对，不直接再发查询。核验结果通过 append_fact/resolve 条件更新，不重置累计次数。

### S8 方法级验收与交付清单

| 规格责任 | 必需验证 |
| --- | --- |
| ToolFactSink / batch | revision 去重、先成功后取消、关闭 collector 后迟到引用释放、普通失败不抹兄弟、硬取消下 Integration 事实仍有 Owner；同原操作的多个消费 call 独立配对，未决复用无订阅/新执行 |
| ToolAdmission | 0/满/撤回/取得同刻取消、两 run 多 batch 配额、等待写防饿死、无关资源进展、多资源无循环等待、拒绝递归调用 |
| Supervisor / Handle | 吞取消迟回值、真实线程未完不退容量、清理期限、接管满、不在事件循环执行阻塞工作、关闭依赖顺序 |
| 适配器 | 安全写前检查、部分写 NONE 误判防护、只读分类可信、相邻/重叠根跨工具冲突、code_exec 限制测试不冒充沙箱 |
| 存储 / Owner | CAS 乱序、重复 event、迁移失败回滚、DB 中断意图零执行、单机双进程排他、强退重启未知保护、累计核验次数不重置；核验计数提交不明不重复查询，业务键参数/指纹版本不一致拒绝复用 |
| 现有消费者 | 同会话两个 run 不串 Event/成果，stop 会话覆盖其活动 run；Planner/Reflection 子跑不重置期限；实例并发拒绝；tool history 合法 |

P0-A/B 的接口和文件已定位；配置/部署初始值是实施规格选择，不是负载验证结果。实际实现必须以红测及 A/B 分阶段证据更新文档。若实现证据迫使上述职责或公开契约变化，回到对应条款评审，不能以代码方便为由静默改写。

## Consequences

收益：终止不抹除事实；并行部分结果可保留；单一对象失败不拖垮工具；资源清理和未知副作用有明确所有者。

代价：工具端口、适配器事实、持久存储及生命周期需要协同迁移；需要账本的调用依赖存储可用性，普通只读不统一承担该依赖；接管容量耗尽会降低准入；某些通用写工具缺少可信资源范围时不能获得完整隔离保证。两批分别验收，不能将第一批完成标为跨重启保护已兑现。

升级触发：实际多 worker/共享目录需要跨进程原子准入及可信资源身份；真实重复业务需要稳定幂等键；持续服务故障才评估熔断；人工恢复量形成证据后再评估自动补偿。以上不是本期默认建设项。

## 实现与验证证据

Piece①已分离真实调用与后处理异常范围，并把工具重试改为适配器显式安全声明准入；详见
[TOOLS-050](../../../issues/integration/tools/2026-09-14-executor-postprocessing-retry.md)。Piece②已落地
`ToolCallContext`、`ToolFact`/`ToolFactSink`、`ToolBatchCollector` 与三类共享控制异常；Application
创建 run 身份并贯穿 Agent、三种策略、Gateway 与 Integration，现有直接调用者及测试替身已迁移。
同会话多 run 取消隔离、实例并发拒绝、控制优先级、事实先接管再传播和无效 call ID 零执行由
专项及跨层测试验证，当前映射见 [ALIGNMENT](../../../docs/ALIGNMENT.md)。

以上只证明 Piece①、②的进程内接口和纵向接线。共享公平准入、真实线程/进程句柄、迟到事实
监管与释放、并行兄弟有界收尾、最终协议历史和持久恢复仍分别属于 Piece③～⑧；在对应真实
资源和存储测试闭合前，不宣称交付 A/B 或整份 ADR 已全部实施。后续进度与验收命令见
[todo](../../../docs/todo.md#c-02-implementation-pieces)。

2026-09-19 补充实施：WriteFileTool 完整同步工作进入受控线程并声明 MAY_WRITE；当前正式网关落实 A/B 启用门禁，只导出/执行明确只读且不要求强制审计的工具。写适配器修复不代表 B 已完成；不新增绕过配置。原因和验证见 [TOOLS-057](../../../issues/integration/tools/2026-09-19-write-thread-ownership.md) 与 [TOOLS-058](../../../issues/integration/tools/2026-09-19-unprotected-tool-enablement.md)。

## 关联及历史条款承接

- [TOOLS-ADR-004](2026-08-17-tool-timeout-priority.md)：保留 timeout 默认值选择优先级；本决定增加总 deadline 上界，不再把“无 cap”解释成可越过运行总期限。
- [TOOLS-ADR-006](2026-08-17-tool-lifecycle-paradigm.md)：保留连接池及 on_load/on_unload；补齐在途所有权与有界关闭。无状态不证明远端幂等，不能作为自动重试依据。
- [TOOLS-ADR-007](2026-08-17-tool-error-code.md)：保留错误归因用途；未来控制出口及副作用确定性不能仅由六种旧错误码表达，实施时同步公共契约。
- [ADR-003](../../2026-09-12-sdk-call-guard-response-commit.md)：承接调用前准入、调用后先接管再判 Guard，不改变 Reflection 已接受的自查例外。
- [工具模块索引](README.md)、[工程工作流](../../../docs/engineering/project-workflow.md)、[纠正经验](../../../docs/lessons.md)。
