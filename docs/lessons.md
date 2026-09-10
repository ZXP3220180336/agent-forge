# 研发教训

## 2026-09-09 TimeoutError 跨层来源判定（REASON-014）

- **异常类型不能证明控制信号来源**（REASON-014）：`asyncio.timeout_at` 的硬墙、provider 传输、结算和日志都可能抛内置 `TimeoutError`。创建 timeout 范围的一层应保留上下文对象，并以 `expired()` 确认本次硬墙是否实际到期；仅比较当前时钟或只看异常类名都会误分类。
- **异常分类修正必须连同部分状态所有权一起审查**（REASON-014）：把内部 `TimeoutError` 从 TIMEOUT 改为 UNKNOWN 只修了标签；若 UNKNOWN 仍只读取上一完成轮，当前续接已产出的内容和 usage 依然丢失。跨可中断 await 的 `current_result` 必须在所有异常终态统一接管。

## 2026-09-09 ReAct deadline 部分成果与清理窗口（REASON-012/013）

- **部分成果的所有权要在可中断 await 前交接**（REASON-012）：流式 `StreamResult` 在 `async_generate` 返回前就会被逐步填充；只在正常返回后赋值 `last_result` 会让 deadline 出口看不到当前轮。显式区分 `current_result` 和 `last_result`，终止时优先有可见内容的当前轮，空当前轮则保留上一轮。
- **usage 异常载体与部分结果是候选来源，不是两笔消耗**（REASON-012）：异常已携带 usage 时优先采用，否则回退 `current_result.usage`；两者叠加会双计同一请求。
- **协作式 deadline 和强制式 timeout 必须有时序层次**（REASON-013）：同刻触发会让硬取消打断 close/settle。在既有总硬上限内为内部 deadline 预留有界清理窗口，既保留资源收尾时间，也不放宽对外执行契约。

## 2026-09-09 流式 deadline 出口 usage 口径（LLM-047）

- **usage 传递判据 = 「该出口是否可能携已获用量」，不是「统一带或统一裸」**（LLM-047）：流已读 / 已结算的终止出口（整流/续接退避、放弃/续接链、读取期）必须携带 `result.usage`——漏带 = 已耗 token 不进成本总账；请求未发 / 未读流的出口（attempt 入口、create/reserve 段）不带——防御性携带是恒 None 噪音，且掩盖判据。原语层（无 usage 来源）一律裸抛，由最近的读取捕获点统一补全。详见 [LLM-047](../issues/integration/llm/2026-09-09-deadline-usage-propagation-closed-loop.md)。
- **对称出口必须同一口径，修复一个就镜像检查另一个**（LLM-047）：整流退避「被 deadline 中断」重建携 usage、同一退避「睡满复查」却裸抛即缺陷——两者共享同一 usage 来源与取值窗口。
- **usage 会被 `_reset_dead_meta` 清空，判据要对着清空点想**（LLM-047）：整流 continue 前 / 续接 create 前会清 usage——整流 attempt≥1 的 create 段恒 None（裸抛正确），上一 attempt 已读 usage 只在 reset 前的整流退避段可带。跨层 deadline 测试另须给 attempt0 整链前置（tiktoken 估算等）留 deadline 缓冲、固定整流退避参数，否则命中点漂到 attempt 入口裸抛（测不到退避段）。

## 2026-09-09 执行控制迟回值接管（LLM-045）

- **abort 决定业务终态、迟回值决定资源所有权，两者正交、不互斥**（LLM-045）：factory 吞掉取消、以值正常收尾时，helper 不得丢弃该值——迟回值是「请求实际已发生 / 资源已取得」的所有权凭证；调用方须**先接管**（settle/cancel/close）**再完成 abort 收尾**，迟回值**不是业务成功**，不能当成功结果返回。
- **helper 返回 ≠ 业务未终止，调用方必须自带 abort 复查**（LLM-045）：`await_with_execution_control` 的返回值也可能出现在 abort 判赢之后——消费该原语的调用方都须在产生外部副作用前复查 cancel/deadline，否则会把迟回资源当成功泄漏。详见 [LLM-045](../issues/integration/llm/2026-09-09-execution-control-late-result-drop.md)。

## 2026-09-08 执行控制贯穿每笔真实 SDK 调用（LLM-044）

- **执行控制检查点锚定「每次真实 SDK attempt 共用入口 + 每次等待」，而非编排层**（LLM-044）：E 把检查放 `_call_generate` 门口一次，generate 内部 retry/fallback/reserve 整流读取仍是黑盒——取消/期限后仍发真实请求。与 REASON-001/LLM-043 同一教训：护栏落真实调用点（`_budget_guarded_call`）与每次等待（reserve 排队/退避/chunk）。
- **结算按「请求是否已开始」划分，不靠调度假设**（LLM-044）：create 调度后请求可能已达 provider——终止须 `settle(None)` 保守而非 cancel 全额退（防配额虚增→429）；内部 deadline 与外层 asyncio.timeout 到期时间近似，须显式 `create_started` 状态而非赌"内部总先触发"。
- **整体期限不得以内置 `TimeoutError` 表现**（LLM-044）：它会被 classify RETRYABLE 当网络超时重试/整流/续接；终止用类型化私有信号并在 Facade 边界翻译 shared 领域出口（Domain 不依赖 integration 私有）。
- **新增控制参数要沿所有再调用分支逐点核验**（LLM-044 审查补充）：入口签名有 `deadline` 不代表内部闭环完整；首次流式 `retry.execute`、整流、半流续接、fallback 都是独立再调用边。测试应分别让期限落在退避与读取竞态中，并断言 SDK 调用次数，而不只断言最终异常。

## 2026-09-08 generate_structured 取消/期限闭环（LLM-043）

- **执行护栏锚定「真实请求前」而非「策略阶段入口」**（LLM-043）：结构化三级降级链是单次 Facade 调用内的多条真实请求——reflection/planner 的阶段入口 guard 覆盖不到链内部，取消/超时后仍空烧降级/回喂/扩容。护栏粒度与 REASON-001 同一教训在集成层重现：凡「一次编排内多次真实请求」的组件（降级链/重试），护栏检查点必须落在每次请求的统一入口，而非编排层入口。
- **内置 `TimeoutError` 是执行终止信号，不是网络可恢复超时**（LLM-043）：`asyncio.timeout` 到期抛的就是内置 `TimeoutError`；凡 `except Exception` 兜底后按 `classify_error → RETRYABLE` 处理的层，都会把整体 deadline 到期吞成「可恢复失败 → 降级再调用」——termination 语义丢失、超时后仍付费。网络可恢复超时用 `APITimeoutError` / `httpx.TimeoutException` 表达，二者勿混；终止信号必须在 catch 边界直抛（todo §4 明文约束）。

## 2026-09-06 主副模型请求守卫（LLM-041）

- **置熔断状态 ≠ 触发该状态行为**（LLM-041）：手动 `_state = OPEN` 后若 `_last_failure_time` 已过 `recovery_timeout`，`allow_request` 自动转 `HALF_OPEN` 放行主链路探针——测试实际走探针主链路而非 fallback。原「fallback 共享主窗口」绿灯正是因此误测成主链路预算闸（断言 `model_key == "fast"` 掩盖「从没走到 fallback」）。设被测条件须连状态机派生条件一起置（`_last_failure_time` 置向未来令冷却未过）。
- **绿灯固化错误前提比红灯更危险**（LLM-041）：共享主窗口作期望断言后，「备用窗口独立」反而缺测试；重构前先按目标语义重写断言，不迁就绿灯。
- **兜底链路仍是真实付费调用**（LLM-041）：fallback 的窗口/配额/取消/结算与主请求同权——窗口是模型实体属性（同端点 ≠ 同窗口），配额独立池为默认（共享合并待供应商核实），预算拒绝在 retry 直抛不包主网络 cause（否则被当可恢复错误触发再调主）。

## 2026-09-02 LLM usage 计量口径（LLM-038 已修 / LLM-039 口径记录）

- **成本计量 = 全程真实消耗，护栏才成立**（LLM-038）：凡「多次真实调用 + 一次返回」的编排（降级链 / 截断重试 / 回喂），用量回填必须在**每次调用的统一出口累加**，不能挂在成功返回点回填单次——只回填最后一次成功会让 reflection 的 `cost_limiter.check` 低估实际开销，成本超限不触发停机。
- **回填须取自「产出该结果的那次调用」**（LLM-038）：跨循环引用初始变量（`result`）而非最新调用（`retry`）是隐蔽变量错位，只在「回喂后成功」路径触发——无单测锚定则永不复现，新增行为必须带回归护栏。
- **空 dict 是合法累加起点**（LLM-038）：累加辅助函数判空须区分 `None` 与 `{}`（`not target` 会把首个调用的累加静默吞掉）。
- **「浪费 token 没计入」先分「数据可得 / 不可得」再谈修**（LLM-039 分界）：语义层失败（调用成功但内容不可用）数据可得 → 是缺陷要修（LLM-038）；传输层失败（异常/中断/final 主调用失败）数据不可得 → 是口径要记录，不修。把不可得的当缺陷修，要么修不了（usage 只随成功响应返回），要么把 tiktoken 估算值混进 usage 污染下游成本报告的可审计性——失败消耗的估算应放观测侧独立事件（LangSmith/LiteLLM 惯例），成本护栏只用精确列。

## 2026-09-01 Reflection 异常面收口（REASON-010）

- **验证调用链完整出口，别凭 grep 断言异常冒泡**（REASON-010）：评估异常处理只看到「某处 raise 某异常」不够——必须追踪调用链每一层是否捕获 / 短路 / 重新抛出。本次误判 `StructuredTruncationError` 会冒泡到 Reflection，实际 `StructuredOutput.extract` 入口三处 `except ... return None` 短路，异常根本不达上层；凭 grep 到 `_raise_boundary` 的 raise 就定性，漏了 extract 外层的短路。
- **异常捕获范围锚定异常树根，而非枚举已知错误**（REASON-010）：集成层异常面会演进（新增截断类别、不可恢复错误上抛），按具体类型枚举会随演进漏项。业务 / 系统级错误统一捕获树根（`AppError`），编程错误留待冒泡（fail fast，不掩盖 bug）。
- **降级承诺按「每条失败出口」逐条核验**（REASON-010）：组件承诺 best-effort（不抛错），就要穷举其依赖组件（LLM 网关）的所有失败信号出口——返回 None / 抛 Structured 异常 / 抛不可恢复异常——逐条确认落入降级路由，而非只覆盖「当前已知」的少数几种。
- **统一异常树的价值依赖「出口归一」闭环**（REASON-010）：`AppError` 树在领域层兜底的前提是集成层把所有透出边界的异常都归一进树——`llm_service.generate` 对 NON_RETRYABLE 直接 `raise` openai 原始异常（`APIStatusError` 非 AppError），领域层 `except AppError` 就兜不住 4xx / 认证。异常归一是集成层职责，勿在领域层 import openai 类型（违反依赖方向）。

## 2026-08-30/31 Agent 异常处理与 React 策略评审

- **协议一致性校验要看全维度**（REASON-004/006）：`tool_calls` 信号不仅要与「数据非空」一致，还要与「工具可用性（`has_tools`）」一致——任何一侧缺失都是协议异常，不能只校验数据一侧（004 只查了数据侧，遗留能力侧）。
- **护栏失效比没有护栏更隐蔽**（REASON-006）：误入空输出分支时，非空 `tool_calls` 恰好清零重试计数——护栏「以为模型有产出」而静默失效，行为看似正常直到 `max_iterations`。
- **护栏应对称 + 扩展点护栏在扩展点侧生效**（REASON-007）：同类重试型错误（空输出/LLM 失败/停滞）应有同构独立上限 + 硬终止；默认行为（STOP）不暴露问题，但 handler 显式 CONTINUE 是开放扩展点——护栏必须在 handler 决策分支内生效（即使 CONTINUE 也硬终止）。
- **扩展点异常必须隔离**（SHARED-001）：用户 handler/回调/插件是外部扩展点，其缺陷不破坏核心链路——捕获 + 记录 + 降级默认；**兜底路径本身不能被可抛异常的扩展点击穿**（UNKNOWN 兜底在主循环 except 内，若 dispatch 依赖可抛异常的 handler，兜底会失效）。
- **`except Exception` 与 `BaseException` 的边界**（SHARED-001）：防御性捕获用 `except Exception` 而非 `except BaseException`——`asyncio.CancelledError` 等取消/退出信号必须穿透（对齐「CancelledError 永不吞」）。
- **取消是独立语义，不混入失败分类**（REASON-003）：用户取消（CANCELLED）与 LLM 失败（LLM_FAILED）必须分离——否则 handler 的 CONTINUE（重试）会作用于取消，产生「用户点停止却在重试」的荒谬行为；靠 `cancel_event.is_set()` 判别，不依赖 error 文本（避免字符串耦合）。
- **截断 ≠ 脱敏**（REASON-005）：`[:200]` 只限长度，不挡敏感值（可出现在前 200 字符内）；脱敏按「语义剔除」（只留异常类型名）而非「长度裁剪」；**兜底路径的泄漏面最广**（UNKNOWN 捕获所有未捕获异常，其 error 必须最保守）。
- **产品可见文本与运维诊断必须分离**（REASON-005）：error / SSE / 根因报告是产品侧（面向良率工程师），异常堆栈 / message 是运维侧——两套文本，敏感数据不泄露、诊断能力不损。LLM_FAILED 保留失败原因（401/429/超时对诊断有价值）不脱敏，UNKNOWN 无诊断价值才脱敏——取舍按「诊断价值」而非一刀切。
- **写入历史要有「产出守卫」**（REASON-008）：任何「追加进消息历史」的操作应校验本轮是否有可回馈信息——空输出轮（模型什么都没说）写历史既无益又污染；守卫用「信号存在」（`has_reasoning=True` 的 thinking 空 reasoning 轮是有信号的）而非「内容非空」，避免误伤 thinking 场景。
- **文档滞后于代码演进是常态**（职责划分评估）：08-30 三批能力（成本护栏/优雅取消/停滞检测）落地后，executor.md / agent.md / react.md 停留在 08-29，造成构造契约缺参、异常契约计数过期等 8 处漂移——能力落地后应同步文档（尤其对外接口契约与配置项）。
- **文档「写当前状态」也适用于问题记录**（REASON-006 合并）：同一机制的两类触发（协议异常数据侧/能力侧）应单档案记录，合并后「一个事实一个家」，避免两个文件描述同一机制必然漂移。

## 2026-08-17 工具模块重构（六大子组件对齐）

- **「看似 bug」需实测语义再定性**：`except json.JSONDecodeError, KeyError:` 初判为 Python 2 语法 bug，实测 Python 3.14（PEP 758）下语义 = 元组捕获（运行时正确）——异常语法 / 语义疑似 bug 时，先实测当前解释器行为再定性，勿凭版本印象下结论。
- **head+tail 截断后长度 ≠ max_length**：截断结果 = head + marker + tail，marker 占额外空间必然略超 max_length。测试断言不能写 `len(content) <= max_length`，应断言 head 开头 / tail 结尾 / marker 存在 / 远小于原始。
- **文档跨目录相对链接要算准路径**：`docs/integration_doc/tools_doc/` 下的子文档引用 `adr/` 需 `../../../` 前缀（三级到根），漏前缀会被 `verify_alignment.py` 死链校验拦截（报「死链」而非解析错误）。
- **verify_alignment 的强制项**：`app/` 下每个非空 `.py` 必须在 ALIGNMENT.md 登记；✅ 状态必须同时有非空文档 + 非空测试；文档路径必须真实存在。新增组件文件后不同步 ALIGNMENT 会直接导致 `test_verify_alignment.py` 失败。
- **async 冒烟脚本要 await**：`Container.initialize()` 是 async，`python -c` 冒烟需 `asyncio.run(main())` 包裹，否则 `RuntimeWarning: coroutine never awaited` + tool_service 为 None。
- **jsonschema 中文模板断言防笔误**：测试断言用「必须 ['fast','slow'] 之一」写错为「必须 ['fast','slow'] 之一」少个「是」导致 false negative；断言直接用代码 `_map_error` 生成的准确措辞。
- **审计落点独立于 Hooks**：审计须覆盖未注册 / JSON 失败 / 校验失败 / 成功 / 失败全路径，而 ExecutionHooks 仅成功路径——两者职责不同不能复用；未注册工具审计要保留原始工具名（`_audit` 加 `tool_name` 参数），不能硬编码兜底。

## 2026-08-15 文档/测试以代码架构为准对齐

- 文档目录迁移后，批量替换链接时不能只匹配带 `docs/` 前缀的路径：文档内互链常用不带前缀的相对路径（如 `service_doc/llm_doc/llm.md`），需同时替换两种形式。
- 批量替换要按「最长最具体 → 最短最通用」排序，否则 `app/services/` 这类通用前缀会先吃掉子路径，导致错误映射。
- `apply_patch` 一次补丁不能对同一文件拆成两个 `Update` 块；遇到同一文件多处改动要合并到一个块或分次提交。
- 沙箱/策略可能拦截 `Remove-Item` 等破坏性命令；空目录不影响 git 追踪，优先保证工作区内容正确，清理可后置。

## 2026-08-15 补齐缺失测试（第 7 项）

- 手写 fake DB 语句分发时，不能用 `column_descriptions[0]["entity"]` 区分实体行查询与聚合查询：聚合 select（stats/count）的 entity 也是 FROM 映射类。改用 `descs[0]["expr"]` 是否为映射类（type）判定。
- fake 的 `db_session_factory` 返回值必须实现异步上下文管理器（`__aenter__/__aexit__`），因为业务侧用 `async with self.db_session() as db`。
- 构造参数名 ≠ 实例属性名：`SessionManager(db_session_factory=...)` 存为 `self.db_session`，写测试断言前先看源文件的属性赋值。
- 复用 fake 要清理状态：`list_sessions` 首页会写 Redis 缓存，连续调用需清空 fake redis，否则二次调用命中缓存不查库、断言落空。
- 内置工具测试要显式 `register_config(api_key="")` 重置 key：仓库根 `.env` 含真实 TAVILY_API_KEY，不重置会触发真实网络请求。
- pydantic-settings 构造用 `Settings(_env_file=None)` 可跳过 `.env`，使配置测试确定性（不依赖环境）。

## 项目级教训（自 HANDOFF.md 迁移，2026-08-15 归档）

> HANDOFF.md 移除后，原「研发教训」中未沉淀到模块文档/CLAUDE.md 的条目迁移至此。

- **`__init__.py` 文件名笔误**：写成 `__ini__.py`（少个 t）会导致 `ImportError: cannot import name 'XX' from 'app.models' (unknown location)`。出现 `(unknown location)` 的导入报错，**先检查 `__init__.py` 文件名**。
- **markdown 中文表格 lint**：markdownlint 的 MD060 按字符宽度（中文算 2 格）校验表格对齐，手写中文表格极易误报。用脚本按 east_asian_width 计算列宽自动对齐。**新改表格后重跑对齐脚本。**
- **文档移动后必须同步交叉链接**：文档目录重组后，`architecture.md` 的「相关文档」链接已同步修正，但**检查其他文档中是否仍有指向旧路径的链接**（如曾引用 `config.md` 旧位置）。
- **路由导入路径与文件结构不一致**：`api/routes/` 文件名与实际导入名不一致会 ImportError；路由内引用应匹配 `app/` 下的真实模块位置。**`__init__.py` 的导入名要匹配实际文件名，跨层导入用绝对导入 `from app.xxx import ...`。**
# 2026-09-10 续接准入失败与超时契约边界

- **下一阶段资源未取得前，不清理上一阶段的终止状态**：半流续接在预算闸、取消或 deadline 处可能尚未取得新流；此时旧流的 content/usage 仍是当前运行的有效成果。元数据复位必须放在 `continue_fn` 成功返回之后。
- **超时触发点不等于绝对返回时限**：`asyncio.timeout` 到期会取消 task，但同步阻塞或吞取消代码仍可能延迟返回；timeout scope 外的领域收尾也形成尾部。文档必须明确 scope、取消协作前提和尾部是否允许新副作用。
