# 2026-09-01 Reflection 反思循环终止护栏（P3）：cancel + 超时降级

> 评审发现：Reflection 自查/修正循环只有 cost_limiter 一重护栏，`cancel_event` / `max_execution_time` 只透传 react 收集阶段——用户取消或超时后仍发起付费调用（与 ReAct 三重护栏不对称）。

- [x] reflection.py：`_should_abort`（用户取消 / 总时长超限检查）+ execute 循环顶部调用 → 停机降级采用最近稿（复用 `_finalize` 降级路径）
- [x] 测试：`_should_abort` 单元 3 例（取消 / 超时 / 正常）+ 循环中取消降级集成 1 例；test_reflection 30 + agent 5 通过
- [x] 文档：reflection.md（测试状态 26→30 / 边界情况补 cancel + 超时）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-09-01 Reflection done 事件口径修复（REASON-011 / P2）+ 抑制 ReAct 中间 done

> 评审发现：`_finalize` 的 done 事件只用 react 阶段 total_tokens，漏计 critique/refine 用量——SSE 事件与 outcome 两个事实源漂移（成本审计失真）；且 Reflection 透传 ReAct 的 done 造成事件流 2 个口径不同的完成事件（噪音）。

- [x] 修复 reflection.py：`_finalize` 的 `build_done_event` total_tokens 改为 `react + _structured_usage`（与 outcome 一致）
- [x] 抑制 ReAct 中间 done：Reflection 透传 ReAct 事件时过滤 done（`AgentEventType.DONE.value` 匹配），事件流仅保留收尾 1 个 done
- [x] 测试：`test_reflect_done_event_tokens_match_outcome` + `test_reflect_suppresses_react_done_event`；test_reflection 26 + agent 5 通过
- [x] 文档：reflection.md 测试状态（22→26）+ issue REASON-011（教训 2 更新为已修复）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-09-01 llm_doc/error.md：传输错误处理组件文档 + 文档链接收敛

> 用户要求 llm 异常处理文档同步：新建 error.md（组件模板 B），其他文档异常相关说明链接到它（一个事实一个家）。

- [x] 创建 `docs/integration_doc/llm_doc/error.md`（分类 / 归一 / 降级判定 / 下游决策 + 契约归属 + 测试 / ADR / issue）
- [x] retry.md：错误处理两节缩减为链接 error.md（目录 / 架构表 / 对外接口 / 测试状态同步）
- [x] llm.md：模块结构 + 内部组件表 + 对外异常契约链接 error.md
- [x] llm_service.md / structure.md / streaming_rectifier.md：异常说明链接 error.md（顺带修正 `_is_unsupported_response_format_error` 旧名 → `is_unsupported_response_format_error`）
- [x] ALIGNMENT：errors.py 文档列 → error.md；层 README 结构图 / 组件表 / 相关文档登记 errors.py
- [x] 验证：verify_alignment 通过

---

# 2026-09-01 ErrorCategory 契约归属修正：shared → llm/errors.py

> 上移 shared 后复核发现：ErrorCategory 是 LLM 传输层分类，仅集成层 LLM 消费（领域/应用层实际不引用），放 shared 是过早泛化（shared = 被所有层引用的零依赖核心库）。契约与实现同归 `app/integration/llm/errors.py`。

- [x] errors.py：ErrorCategory/ErrorClassifier 契约从 shared/error_category.py 移回本模块（契约与实现同层，单一消费方）
- [x] 删除 app/shared/error_category.py + adr/shared/error_category/（上移决策被修正）
- [x] import 源收敛：retry / streaming_rectifier / test_classify_error / test_error_category → app.integration.llm.errors
- [x] 文档：ALIGNMENT（删 error_category 行 + 更新 errors.py 行）/ retry.md / error_handling.md / issue REASON-010
- [x] 测试：全量 pytest + verify_alignment 通过

---

# 2026-09-01 llm 模块异常处理收敛：新建 llm/errors.py（错误知识 + 决策 helper）

> 用户指出 llm 模块异常处理散乱（分类/归一/降级判定散在 retry.py + structured.py，4 处消费方各自决策）。收敛：新建 `app/integration/llm/errors.py` 作为传输错误处理单一归属 + `decide_downstream_error` 统一 generate 下游决策。

- [x] errors.py：迁移白名单 / `classify_error` / `normalize_transport_error` / `is_unsupported_response_format_error`（自 retry.py / structured.py）+ 新增 `DownstreamDecision` / `decide_downstream_error`
- [x] retry.py：删迁移符号，从 errors 导入 classify_error（保留熔断/重试机制）
- [x] llm_service.py / structured.py：generate / _call_generate except 改用 decide_downstream_error；unsupported 400 降级下一级特判保留 structured（降级链私有语义 + 诊断日志）
- [x] streaming_rectifier.py：classify_error import 源收敛
- [x] 测试：新增 test_errors.py（16 用例：normalize / unsupported / decide 决策矩阵）；test_classify_error / test_error_category import 源更新；全量 755 passed
- [x] 文档：ALIGNMENT（登记 errors.py + 更新 retry/llm_service 行）+ retry.md（错误处理节归属与决策描述）
- [x] 验证：verify_alignment

---

# 2026-09-01 集成层 openai 异常归一（LLMAPIError）+ ErrorCategory 契约上移 shared

> 用户批准归一方案（REASON-010 遗留闭环）并新增「契约上移 shared」决策，实施完整重构（TDD）。归一目标：领域层 `except AppError` 统一兜住集成层透出的不可恢复错误。

- [x] **契约上移**：新建 `app/shared/error_category.py`（ErrorCategory + ErrorClassifier 类型别名 + 契约 docstring，零依赖）；retry.py 删本地定义改 import shared，classify_error 标注结构实现契约；llm_service/structured/streaming_rectifier/test_classify_error import 源收敛（grep 全量核对）
- [x] **异常归一**：`app/shared/exceptions.py` 新增 `LLMAPIError(NonRetryableError)`（code=LLM_API_ERROR，携带 status_code）+ `AppErrorCode.LLM_API_ERROR`；retry.py 新增 `normalize_transport_error`（包装 APIStatusError + 非 HTTP 永久性异常，其余 None）；llm_service.generate except 边界 `raise LLMAPIError(...) from e`（非 openai 原样透传）
- [x] **流式不归一**：async_generate 保持 StreamResult.error 字符串语义（无异常逃逸，不构成缺口）
- [x] **status_code 硬约束**：`_is_unsupported_response_format_error` 依赖 status_code==400 + message 关键词 → 保留两属性，response_format 400 降级链存活（test_generate_structured 既有用例保护）
- [x] **TDD（先写失败测试）**：test_reflection.py 新增 LLMAPIError(401/403) 自查/修正降级 2 用例（修复前红色：模块缺失）；test_llm_service.py 新增归一 4 例（401→LLMAPIError + cause 链 / APIResponseValidationError→status_code=None / ValueError 透传 / APITimeoutError→return None）；test_error_category.py 契约归属 3 例
- [x] **测试**：受影响 189 passed → 全量 740 passed（test_verify_alignment::test_current_repo_passes 在 ALIGNMENT 更新后转绿）
- [x] **文档同步**：error_handling.md（异常清单 LLMAPIError/全景/传播链/四码边界）+ retry.md（契约位置 + normalize_transport_error 节）+ llm_service.md（边界 5）+ llm.md（对外异常契约）+ ALIGNMENT.md（新增 error_category 登记 + 更新 3 行 + 日期）
- [x] **ADR**：`adr/integration/llm/2026-09-01-openai-error-normalization.md`（归一决策）；`adr/shared/error_category/2026-09-01-error-category-contract-up.md` 已删除（契约归属决策被修正，见顶部「契约归属修正」条目）
- [x] **issue 收尾**：REASON-010 遗留标记闭环 + 「遗留闭环」节 + reasoning README 索引登记（REASON-010）
- [x] **验证**：全量 pytest + verify_alignment 通过

> 评审要点：归一位置选 generate 边界而非 retry 层（retry 内部对原始异常分类/记账零扰动）；不复用 UnauthorizedError/ForbiddenError/NotFoundError（BusinessError 承载 API 会话语义，避免混淆）；error_handler 映射 LLM_API_ERROR→502（不映射 401 防误导客户端会话失效）；reflection.py 零代码改动（except AppError 已就位）。

---

> 评审发现：`_critique`/`_refine` 只捕获 StructuredRefusalError/StructuredToolCallError，漏掉会冒泡的不可恢复错误（熔断 CircuitBreakerOpenError 等 AppError）——违反「不抛错降级」承诺。修复：except 扩展为 AppError（异常树根），编程错误仍冒泡不掩盖。迭代核实：截断（StructuredTruncationError）由集成层 StructuredOutput.extract 短路返回 None，非本层缺口，删除虚假锚定测试；openai 4xx/认证未归一 AppError，留集成层遗留。

- [x] issue：`issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md`（REASON-010）
- [x] TDD：新增 3 用例（自查/修正抛不可恢复 AppError → 降级；编程错误仍冒泡）——初版含 2 截断用例，核实 extract 短路返回 None 后删除
- [x] 修复 reflection.py：`_critique`/`_refine` 的 except `(StructuredRefusalError, StructuredToolCallError)` → `AppError`
- [x] 测试：test_reflection.py（22）+ test_reflection_agent.py（5）通过；全量 pytest 无回归
- [x] 文档：reflection.md（降级路由/边界情况/测试状态/错误分发）+ reflection_benchmark.md（#9）；ALIGNMENT 无模块结构变化，校验通过
- [x] 验证：verify_alignment 通过

---

# 2026-09-01 Reflection 自查/修正 token 统计 + cost 护栏（benchmark #11 去 ⚠️）

> generate_structured 回传 usage（仿 async_generate result 模式），critique/refine 纳入 token/成本统计 + cost_limiter。

- [x] 端口 llm_gateway.generate_structured 加 `usage` 可变参数（返回签名不变，向后兼容）
- [x] LLMService / StructuredOutput（extract / _try_extract / _fallback_extract）透传 + 回填 usage
- [x] ReflectionStrategy：_generate_* 返回 (result, usage)；累计 _structured_usage → outcome.total_tokens/usage 合并；循环顶部 cost_limiter.check 超限停机降级
- [x] 测试：新增 usage 累计 + cost 超限 2 用例；假对象适配 usage 参数；全量 728 passed
- [x] 文档：ADR（cost 缺口 → 完整）/ benchmark #11 ✅ / reflection.md / ports.md / structure.md / llm.md / llm_service.md

---

# 2026-09-01 Reflection 修正循环缺陷修复（REASON-009）

> 审查发现：修正循环复用同一批 issues 反复修正（工业反模式）。调研确认「迭代必须新反馈」后重构为真迭代。

- [x] issue：`issues/domain/reasoning/2026-09-01-reflect-refine-loop.md`（发现→分析→修复→验证→教训）
- [x] 重构 reflection.py：阶段二+三改为 while 真迭代（修正 refined → 重新自查 refined → ok 采用 / 新 issues 再修正 → 达上限 best-effort）
- [x] 达上限判断修正：`refine_round >= max_refine_rounds - 1`（max=1 时初稿有 issues 即不修正）
- [x] 测试：新增真迭代 2 用例（复查新 issues 再修正 / 达上限采用最后稿）+ 更新 issues_refine 测试，22 passed
- [x] 文档：ADR（re_critique 决策改为真迭代 + REASON-009 链接）/ reflection.md / reflection_benchmark.md（#5/#17）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-31 React 策略完成度/职责划分评估 + 文档漂移同步 + 遗留问题收尾

> 两项子智能体评估（完成度 26 项对照 + 策略/编排职责划分）→ 文档漂移同步 4 份 → 未解决问题清单 #1~#4 逐项修复（REASON-006/007/008 + 取舍标注），全部提交推送。

- [x] **完成度评估**：核心必备 13/13 + 增强 8 项，26 项 ✅21 / 🔶1（沙箱降级）/ ❌4（guardrail/compaction/checkpointer/final_answer_checks 均「不做或预留」）；无过设计，达工业级核心基线
- [x] **职责划分评估**：代码层「策略=算法、编排=生命周期」无越界；文档滞后于 08-30 三批能力（2 高危 + 4 中危 + 数低危漂移）
- [x] **文档漂移同步**：executor.md（`__init__` 契约补 cost_limiter/cancel_event、优雅/硬取消、测试数 5→8）、agent.md（异常契约 9→12 类）、react.md（架构图 12 类、execute 签名、max_turns 文案、终止分支表、reasoning 语义、_finalize_terminal 复用）、react_benchmark.md（#4 文案、#23 计数）
- [x] **问题 #1**（REASON-004 扩展）：无工具 + 非空 tool_calls → 协议异常短路（`or not has_tools`）；REASON-006 并入 REASON-004 单档案
- [x] **问题 #2**（REASON-007）：LLM 失败重试独立上限 `max_llm_fail_retries`（settings→AgentContext→透传→execute 完整链路 + 硬终止，对齐空输出护栏）
- [x] **问题 #3**：LLM_FAILED 不脱敏 vs UNKNOWN 脱敏取舍标注（诊断价值 vs 无诊断价值，文档）
- [x] **问题 #4**（REASON-008）：空输出重试轮不追加空 assistant 消息（纯空轮不写历史）+ AGENT-001 except 元组回归
- [x] 验证：全量 700 passed + verify_alignment；提交推送（c9e7f00 / 0d2bed2 / 5b67e60）

---

# 2026-08-31 Reflection 推理决策 + ReflectionAgent 编排（领域层 Slice 4）

> 以工业级 Reflection 决策为评价指标（reflection_benchmark.md 对标）。三阶段分离 + Grounding（证据锚定，反内在自查）。

- [x] 共享内核：AgentErrorKind 加 CRITIQUE_FAILED（默认 CONTINUE，12→13 类）+ 配置 agent_max_refine_rounds
- [x] `reasoning/reflection.py`：ReflectionStrategy（生成复用 ReAct.execute(output_schema) + 自查 CRITIQUE_SCHEMA + 修正 REFINE_SCHEMA + 降级路由）+ ReflectionOutcome + schema 契约
- [x] prompts：templates/reflection.py（REFLECTION_SYSTEM / CRITIQUE_PROMPT 9 维清单 / REFINE_PROMPT）+ manager builder
- [x] `agent/reflection.py`：ReflectionAgent 桥接（构造注入 + _map_outcome 进 metadata）
- [x] 测试：test_reflection.py（15 用例）+ test_reflection_agent.py（5 用例），全量 723+ passed
- [x] 文档：reflection.md / reflection_benchmark.md（核心必备 12 项对照）/ reasoning.md / agent.md / config.md / ALIGNMENT / domain README
- [x] ADR：`adr/domain/reasoning/2026-08-31-reflection-strategy.md`
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-30 Agent 运行异常处理全面性评审（5 问题逐项修复）

> 评审 Agent 运行异常处理全面性，发现 5 个问题（部分进度 / 取消语义 / 协议异常 / handler 防御 / error 脱敏），按问题修复工作流逐项完成（测试驱动 → issue → 文档 → 停等审核），全部修复并推送。

- [x] 问题 1（REASON-002）：UNKNOWN 未保留部分进度 → `_finalize_unknown` 用 last_result 组装 outcome（对齐 TIMEOUT/COST_EXCEEDED/STALLED 部分进度保留模式）
- [x] 问题 2（REASON-003）：取消信号语义错位 + /chat/stop 未接线 → cancel_event 链路贯通（ReAct 识别 → CANCELLED，不重试）+ TaskService 会话级取消注册表 + chat 真实优雅取消
- [x] 问题 3（REASON-004）：finish_reason=tool_calls 但 tool_calls 空 → 短路 PARSE_FAILED 协议异常分发（不入空输出计数 / 不进空转执行，默认重试）
- [x] 问题 4（SHARED-001）：错误处理 handler 自身异常无防御 → ErrorHandlerRegistry.dispatch 捕获降级默认 action（except Exception，CancelledError 穿透不吞）
- [x] 问题 5（REASON-005）：UNKNOWN error 拼接完整异常文本泄漏内部细节 → 只留异常类型名（产品侧脱敏）+ 完整异常含 traceback 进日志（运维诊断）
- [x] 验证：全量 692 passed + verify_alignment 通过；提交推送至远端（05a3f5d / 问题2 / 9a4ed74 / 91e5ecc / f7b5861 / b8277b7）

---

# 2026-08-30 Token 计量并入 LLMGateway：移除 TokenCounter 端口

> 延续 Facade 收窄原则：Token 计量（模型特定编码）同成本估算一样是 LLM 能力，从独立 `TokenCounter` 端口并入 `LLMGateway`（端口 6→5），`ContextManager` 经 `LLMGateway.count_*` 接入，不再接触独立端口。

- [x] `llm_gateway.py`：加 `count_tokens` / `count_messages_tokens`（LLM 能力归属）
- [x] `llm_service.py`：委托 `TiktokenTokenCounter`（主模型，惰性构建）
- [x] 删除 `domain/ports/token_counter.py` + `__init__` 导出（端口 6→5）
- [x] `context_manager.py`：`token_counter` → `llm`（LLMGateway 端口）
- [x] container：ContextManager 移到 LLMService 之后，`llm=llm_service`
- [x] 测试：test_chat_flow / test_react_strategy / test_container 构造改 `llm=`（位置参数自动绑定）
- [x] 文档：ports.md(5 端口) / context.md / ALIGNMENT / llm.md / token_counter.md / 层 README / types.md / ADR
- [x] 验证：全量 648 passed + verify_alignment

---

# 2026-08-30 LLM 包 Facade 收窄：`__init__` 只导出 LLMService

> 对齐 llm.md 既有原则「LLMService 是唯一对外 Facade，内部组件不对外暴露」：此前 `llm/__init__.py` 全量导出 12 个子组件（ClientManager/CostTracker/RetryHandler/…）与原则矛盾。

- [x] `llm/__init__.py`：收窄为仅 `from .llm_service import LLMService`（对外接口 = Facade）
- [x] `llm_service.py`：包内组件改相对深路径 import（消除依赖 `__init__` 重导出的循环）
- [x] container.py（装配根）：`register_config` 接线改深路径 import（组合根例外，消费方仍只见 LLMService）
- [x] 测试：test_container / test_client_manager / test_stream_rectify 深路径对齐
- [x] 验证：全量 648 passed + verify_alignment

---

# 2026-08-30 成本上限自动停机（增强项 #20）+ 断点续跑 ADR（#19 升级路径）

> 对标收尾：#20 CostTracker 记账有、无 cost ceiling 自动停机。经 CostLimiterPort 端口注入（镜像 ContextBudgetPort），累计成本超限走 COST_EXCEEDED 分发默认 STOP 停机。约束：react.py 只依赖 ports+shared；成本估算是 LLM 能力，归属 LLMGateway.calculate_cost，应用层 CostLimiter 经该端口取成本（不直接 import 集成层子组件）。

- [x] `domain/ports/cost_limiter.py`：CostLimiterPort（check 无状态纯函数，返回 exceeded+cost）
- [x] `domain/ports/llm_gateway.py`：LLMGateway 加 `calculate_cost`（成本估算归 LLM 能力，Facade 结构实现）
- [x] `application/context/cost_limiter.py`：CostLimiter（ceiling + llm 端口注入 + model 配置）
- [x] `error_handling.py`：AgentErrorKind.COST_EXCEEDED（10 类，默认 STOP）+ 默认表 + 测试
- [x] react.py：构造注入 + 每轮 usage 累加后检查（error 判断前）+ `_finalize_cost_exceeded` 降级
- [x] 注入链：settings `agent_max_cost` → container.cost_limiter 单例（llm=llm_service Facade）→ deps.get_cost_limiter → chat → ReActAgent；AgentContext 不改
- [x] 测试：test_cost_limiter 7 例 + react 6 例 + error_handling/container/settings/agent 同步
- [x] 文档：react.md / react_benchmark（#20 ✅）/ ports.md / context.md / config / .env.example / ALIGNMENT / cost_tracker.md
- [x] [ADR #20](../adr/domain/reasoning/2026-08-30-cost-limit.md) + [ADR #19 checkpointer-resume](../adr/domain/reasoning/2026-08-30-checkpointer-resume.md)（升级路径，不实现）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-30 上下文预算放置位置修复（REASON-001）

> 审查发现：预算原放 `_handle_tool_calls` 尾部，仅覆盖「工具调用 → 回喂」路径；LLM 失败重试 / final_answer 回喂重试 / 空输出重试三条非工具继续路径漏裁，上下文无限增长、护栏失效。根因：护栏放置点锚定「工具调用后」而非「LLM 调用前」（横切护栏应锚定其约束的调用点）。

- [x] 测试驱动：新增 `test_react_context_budget_trims_on_no_tool_retry`（空输出重试路径，修复前 assistant=6 失败 → 修复后 ≤3）
- [x] 修复：预算移至主循环顶部（第 0 步，每次 LLM 调用前裁剪，所有继续路径共用）；`_handle_tool_calls` 移除预算与 `max_context_rounds`/`max_context_tokens` 参数
- [x] 文档：react.md（上下文预算节 / 行为边界 / `_handle_tool_calls` 行 / 测试节 30→31）+ [REASON-001 问题记录](../issues/domain/reasoning/2026-08-30-context-budget-placement.md)
- [x] 验证：react 31 passed + 全量 pytest + verify_alignment

---

# 2026-08-29 config 文档重构：移出优化建议为 backlog

> 依 module_doc 规范重构 `docs/config_doc/config.md`，原「后续优化建议」为规划内容（Rule 2 写当前状态），移出登记为 backlog，出现真实需求再落地。

- [ ] 缺失配置项：`AGENT_ENABLE_REFLECTION` / `AGENT_MAX_TOOL_CALLS` / `DATABASE_SSL_MODE` / `DATABASE_CONNECT_TIMEOUT` / `REDIS_MAX_CONNECTIONS` / `LOG_MAX_FILE_SIZE` / `LOG_BACKUP_COUNT` / `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_PERIOD`
- [ ] 默认值优化：`AGENT_TIMEOUT`→600s / `DATABASE_POOL_SIZE`→50-100 / `AGENT_MAX_CONCURRENT_TASKS`→20-50 / `JWT_EXPIRE_MINUTES`→60-480
- [ ] 架构优化：`LLM_` 前缀统一（`MAX_CONTEXT_TOKENS`）/ 配置热更新 / 启动 `model_validator` 依赖检查 / 多环境 `env_file` 模式 / `export_yaml` / `export_json`

---

# 2026-08-28 异常体系完整优化（error_handler 边界 + 命名 + API 收敛）

> 工业级调研（OpenAI/LangChain/Spring AI/SMOL/FastAPI）：分层分类 + 宽松统一基类是正确形态，四套分类（传输可重试/工具系统码/Agent 编排/对外业务码）不应合并。真实缺口是边界未接线 + 命名冲突。

- [x] 调研工业级异常体系设计（统一 vs 分层结论 + 诊断）
- [x] `error_handler.py` 实现（AppError → HTTP 状态 + 统一信封）+ main.py 注册
- [x] `exceptions.py`：加 UNAUTHORIZED/FORBIDDEN/NOT_FOUND 码 + 3 异常
- [x] API 层收敛：deps/session/chat 8 处 HTTPException → AppError 子类
- [x] 命名修正：`AgentError` → `AgentRunError`（解耦语义）
- [x] 测试：error_handler 7 用例 + test_api 信封断言
- [x] 文档：error_handling.md / ALIGNMENT（error_handler ✅）+ [ADR](../adr/shared/exceptions/2026-08-28-exception-system-optimization.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-28 Agent 错误处理策略可扩展（领域层横切，调研 + 决策定稿）

> 对标增强项 #23（⚠️）：先调研工业级详细内容并记录决策文档。四要素「错误分类→注册机制→分发时机→决策动作」；回喂/拦截双层；本项目选 OpenAI 式 ErrorHandlerRegistry + BaseAgent 横切注入。

- [x] 调研工业级错误处理扩展机制（OpenAI Agents SDK / LangGraph / Claude SDK / SMOLagents / LangChain / AutoGen，源码级）
- [x] [ADR](../adr/domain/agent/2026-08-28-agent-error-handling.md)：工业级对照 + 设计方向（AgentErrorKind + Handler 协议 + Registry + BaseAgent 注入）
- [x] 实现：`app/shared/error_handling.py`（共享内核）9 kind 全接入——BaseAgent 注入 + run 分发；ReAct 循环 6 处错误分支改为分发（默认行为零变化）；测试 13 新增（registry 单元 + 分发集成 + run 上抛）

---

# 2026-08-28 结构化输出约束（增强项 #15，Final Answer 工具模式）

> 工业界调研定稿：严格 output_type 会抑制工具调用；选 Final Answer 工具（模型原生结构化 + 循环终止 + 无额外调用）。决策过程（时机/方式/取舍）完整沉淀于 ADR。

- [x] `react.py`：execute 加 `output_schema`，注入 final_answer 工具；识别 → 成功终止（outcome.structured）/ 校验失败回喂（VALIDATION）
- [x] 数据契约：ReActOutcome / AgentResult 加 `structured` + `_map_outcome` 透传
- [x] 测试：3 用例（成功提取终止 / 校验失败回喂 / 未配置不注入）
- [x] 文档：react.md / react_benchmark（#15 ✅）+ [ADR](../adr/domain/reasoning/2026-08-28-structured-output.md)（含讨论概念）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-28 上下文预算管理（核心必备 #10，对标收尾）

> 分层调研定稿：预算管理是横切能力（所有 Agent 模式共享），归 context_manager 统一（复用 TokenCounter），经领域端口 ContextBudgetPort 注入 Agent。双层护栏：轮次（保配对原子）+ token 硬上限。

- [x] `domain/ports/context_budget.py`：ContextBudgetPort 端口 + 登记
- [x] `context_manager.trim_messages`：轮次滑动窗口 + token 预算（复用 TokenCounter，补 tool_calls/reasoning 低估修正）
- [x] 注入链：settings `agent_max_context_rounds=8` / AgentContext 两字段 / ReActAgent 注入 / container / chat
- [x] 测试：context_manager 3 用例（轮次/ token / noop）+ react 集成 + agent_params 同步
- [x] 文档：react.md / react_benchmark（**13/13 完备**）/ config + [ADR](../adr/domain/reasoning/2026-08-28-context-budget.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 reasoning_content 回喂策略（文档更正 + has_reasoning 防御）

> 对标 P2 标注为错误外推（误用 OpenAI o1 规则）——DeepSeek V4 thinking + tools 必须回喂 reasoning_content（否则 400）。补 has_reasoning 信号防御空 reasoning 边界。

- [x] `StreamResult` / `ParsedChunk` 加 has_reasoning；parse_chunk 字段被填充（含空串）即置位
- [x] rectifier `_apply_chunk` 传递；react.py 回喂条件改 `full_reasoning or has_reasoning`
- [x] 测试：parse_chunk has_reasoning 三态 + react 回喂两用例
- [x] 文档：react_benchmark P2 更正移除 / react.md / [ADR](../adr/domain/reasoning/2026-08-27-reasoning-feedback.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 截断标记（P3）

> 对标确认工具结果回喂截断无标记，模型误以为结果完整。修复：截断时追加 `[结果已截断]`（预留标记长度）。

- [x] `react.py`：`_truncate_with_marker` 辅助 + tool 消息（2000）/ SSE（200）两处应用
- [x] 测试：2 用例（长结果带标记不超限 / 短结果无标记）
- [x] 文档：react.md（测试节 16 用例）/ react_benchmark.md（#9 瑕疵移除，P3 完成）+ ADR 并入
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 解析 / JSON 失败降级（核心必备 #7）

> 对标确认解析失败静默降级空参（掩盖错误）。修复：解析失败不执行工具，构造失败 ToolResult（JSON_PARSE）走失败回喂闭环。

- [x] `react.py` _execute_one：解析失败不执行工具，构造失败 ToolResult 回喂 + JSON_PARSE 证据链
- [x] 测试：1 用例（不执行 / 回喂解析失败 / 证据链 JSON_PARSE）
- [x] 文档：react.md / react_benchmark.md（#7 ✅，12 完备 / 0 部分 / 1 缺失）+ ADR 并入
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 工具异常回喂 + 无效工具名处理（核心必备 #5/#6，P0）

> 对标 [react_benchmark.md](domain_doc/reasoning_doc/react_benchmark.md) 确认工具失败信息不回喂模型：失败 `content=""` 回喂空串，模型无法自愈；`error` 未进证据链。修复根因 = 失败信息在两个出口同时丢失。

- [x] `react.py` execute_tool_calls：失败回喂 `str(result)`（"错误: <error>"），成功回喂 content；证据链记录加 `error` / `error_code`
- [x] 无效工具名：NOT_REGISTERED 走同一失败回喂分支，无需额外逻辑
- [x] 顺带修复 AGENT-001：except 逗号语法 → 显式元组
- [x] 测试：`_FailingTool` + 3 用例（失败回喂模型 / 证据链记录 / 无效工具名）
- [x] 文档：react.md / react_benchmark.md（#5/#6 ✅，11 完备 / 1 部分 / 1 缺失）+ [ADR](../adr/domain/reasoning/2026-08-27-tool-error-feedback.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 ReAct 策略总时间上限（max_execution_time）

> 对标 [react_benchmark.md](domain_doc/reasoning_doc/react_benchmark.md)（核心必备 #2）确认时间上限缺失；`settings.agent_timeout=300` 存在未使用，接入为 ReAct 循环总时长护栏。

- [x] `react.py`：execute 加 `max_execution_time`（None=不限）+ `asyncio.timeout` 包循环 + 超时降级（对齐 max_iterations 兜底，判别 finalizer 关闭）
- [x] 注入链：base.py AgentContext 字段 / executor.py 透传 / container.py agent_params / chat.py / deps.py docstring
- [x] 测试：新增 4 用例（首轮超时 / 中途超时保留进度 / 宽松上限 / None 不触发）+ 3 处 agent_params 覆盖同步
- [x] 文档：react.md / react_benchmark.md / config.md + [ADR](../adr/domain/reasoning/2026-08-27-reactor-max-execution-time.md)
- [x] 验证：全量 593 passed + verify_alignment 通过

---

# 2026-08-27 领域层推理决策架构完整建设（Phase C 前置）——总体规划与进度

> 完整计划见 [domain-layer-plan-tmp.md](domain_doc/domain-layer-plan-tmp.md)（临时计划文件，源自 2026-08-27 会话记录）。
> 目标：领域层四模块（策略 / prompt / 记忆 / 编排）建设完整、跑通单 Agent 任务后再上应用层多任务编排（Orchestrator 依赖 PlannerAgent 拆分）。
> 产品锚点：PlannerAgent.plan() = 主 Agent 拆分原语（Phase C 主链路第一步）。

- [x] **Slice 0** ReAct 抽离：reasoning/react.py（ReActStrategy 收标量参数）+ executor.py 变薄桥接（保留 _execute_tool_calls 转发）
- [ ] **Slice 1** Prompts 完整化：planning.py 从 draft 完整化（depends_on / re-plan / 汇总）+ reflection 模板 + manager 3 builder + run() 默认 system 注入
- [ ] **Slice 2** Memory 基座：VectorStorePort + memory/ 六文件（端口 + 骨架，向量实现留 Phase D）
- [ ] **Slice 3** PlannerAgent：agent/planner.py（plan 结构化拆分 / depends_on 拓扑执行 / replan 限 2 次 / summarize 降级）
- [x] **Slice 4** ReflectionAgent：reasoning/reflection.py + agent/reflection.py（✅ 2026-08-31 完成，见当日条目）
- [ ] **Slice 5** 装配接线：container / deps / chat / task_service / base（memory_enabled 默认 False 惰性零行为变化）
- [ ] **Slice 6** 文档 + ADR + 对齐：ALIGNMENT / architecture / 各模块文档 + 4 ADR / verify_alignment

## 关键设计决策

- ReActStrategy 收标量参数 → 遵守 reasoning→ports+shared 依赖方向，策略可独立测试
- PlannerAgent 执行阶段复用 execute_tool_calls 不复用完整 ReAct 循环（plan-then-execute 灵魂）
- generate_structured 返回 None 降级：plan None→ReAct 兜底；summarize None→纯文本；critique None→采用初稿
- 记忆不 mock 向量库：LongTermMemory 端口注入，None 时 no-op（Phase D 接 Milvus）
- 记忆避免孤儿：BaseAgent 完成钩子 + chat 接线（memory_enabled 默认 False 惰性）

---

# 2026-08-27 领域层推理决策架构建设 · Slice 0：ReAct 策略抽离

> Phase C 前置：领域层四模块完整建设（策略 / prompt / 记忆 / 编排），单步执行。
> Slice 0：ReAct 循环逻辑从 `agent/executor.py` 抽离到 `reasoning/react.py`（ReActStrategy），executor.py 变薄桥接。

- [x] `reasoning/react.py`：ReActStrategy + ReActOutcome + execute()（完整循环）+ execute_tool_calls()（工具并行原语）
- [x] `agent/executor.py`：ReActAgent 桥接（_strategy_cycle 委托 + _map_outcome + _execute_tool_calls 转发）
- [x] `reasoning/__init__.py`：导出 ReActStrategy / ReActOutcome
- [x] 新增 test_react_strategy.py（6 用例）：工具循环 stop / error 短路 / 空输出重试 / 迭代兜底 / 并行保序 / 并发
- [x] 文档：reasoning.md（react.py ✅）/ ALIGNMENT / agent.md（executor 桥接定位）
- [x] ADR：`adr/domain/agent/2026-08-27-react-strategy-extraction.md`
- [x] 验证：全量 589 passed（既有 test_agent 3 用例零改动 + test_chat_flow 集成链路通过）

---

# 2026-08-24 Embedding 端口化落地（Phase B 剩余任务 #4，最后一项）

> 架构文档端口层目标：EmbeddingPort。孤儿服务 EmbeddingService 端口化（结构实现），补测试；RAG 接线留 Phase D。

- [x] `app/domain/ports/embedding_port.py`：EmbeddingPort 协议（embed / embed_batch），ports/**init** 登记
- [x] `embedding_service.py`：docstring 标注结构实现 EmbeddingPort（签名已匹配，构造依赖保持）
- [x] 新增 test_embedding.py（9 用例）：单文本/批量保序/分批切批/缓存命中/缓存穿透/disable_cache/空输入/clear_cache
- [x] 文档：ALIGNMENT（embedding_service ✅ + 端口登记）/ architecture（EmbeddingPort ✅、Phase B 全完成）/ embedding.md / domain README
- [x] ADR：`adr/integration/embedding/2026-08-24-embedding-port.md`
- [x] 验证：全量 pytest + verify_alignment 通过

---

# 2026-08-24 types 通用类型落地（Phase B 剩余任务 #3，最小集）

> 架构文档共享内核目标：通用类型 / 标识。最小集只做有真实消费方的类型（SessionId/UserId 标识 + Messages 消息别名），不做 Task 枚举（Phase C）。

- [x] `app/shared/types.py`：SessionId/UserId（NewType）+ Messages（TypeAlias）
- [x] 签名标注：SessionManager 9 方法 + ContextManager.build_messages + AgentContext 字段 + 端口契约（llm_gateway/token_counter）
- [x] 边界保持 str：Pydantic schemas / deps / 路由 / SQLAlchemy Column / dict key
- [x] 新增 test_types.py（4 用例）：NewType 运行时恒等 + 别名等价
- [x] 文档：types.md（新建）/ ALIGNMENT / architecture / todo
- [x] ADR：`adr/shared/types/2026-08-24-type-identifiers.md`
- [x] 验证：全量 pytest + verify_alignment 通过

---

# 2026-08-24 exceptions 统一异常体系落地（Phase B 剩余任务 #2）

> 架构文档共享内核目标：统一异常与错误码。7 个平级散落异常收敛到 `app/shared/exceptions.py`，集成层 re-export。

- [x] `app/shared/exceptions.py`：AppError 根 + NonRetryableError/BusinessError 两支 + AppErrorCode（7 码）
- [x] 收敛：retry/structured/security/validator 删本地定义 + re-export（测试零断裂，相关 135 passed）
- [x] 新增 test_exceptions.py（8 用例）：树结构 / 错误码 / message / ValueError 多重继承 / re-export 兼容
- [x] 文档：error_handling.md（异常树 + 清单表）/ ALIGNMENT / architecture / todo
- [x] ADR：`adr/shared/exceptions/2026-08-24-exception-hierarchy.md`
- [x] 验证：全量 pytest + verify_alignment 通过

---

# 2026-08-24 TokenCounter 端口落地（Phase B 剩余任务 #1）

> 架构约束「零外部框架依赖层」：tiktoken 隔离到集成层。端口 + 实现 + ContextManager 改造 + 测试 + 文档 + ADR 全链路。

- [x] `app/domain/ports/token_counter.py`：TokenCounter 协议（count_tokens / count_messages_tokens），`ports/__init__.py` 登记
- [x] `app/integration/llm/token_counter.py`：get_encoder / content_to_text / TiktokenTokenCounter（tiktoken 唯一使用点）
- [x] `llm_service.py`：`_get_encoder`/`_content_to_text` 迁出 + 别名 import（单一事实源，test_llm_service 零断裂）
- [x] `context_manager.py`：去 tiktoken、注入 TokenCounter、count_* 委托（顺带修复 content=None 潜在 TypeError）
- [x] 测试：新增 test_token_counter.py（8 用例）+ 适配 test_context_manager / test_api / test_chat_flow / test_container
- [x] 文档：ALIGNMENT / domain README / context.md / architecture / token_counter.md / llm_service.md / deps 注释
- [x] ADR：`adr/integration/llm/2026-08-24-token-counter-port.md`
- [x] 验证：全量 562 passed + verify_alignment 通过

---

# 2026-08-20 工具模块代码审查修复（TOOLS-049）

> 来源：工具模块整体代码审查（四维度：正确性 / 安全 / 性能 / 规范）。完整生命周期见 [TOOLS-049 问题记录](../issues/integration/tools/2026-08-20-code-review-fixes.md)。

- [x] **重要项 5**：executor 重试全败归因 / SSRF CGNAT 盲区 / 审计脱敏 error+content / 外部工具冷启动扫描 / RCA 证据链时间锚点（各带回归测试）
- [x] **次要项 15**：executor（execution_time/注释）、loader（_drop_modules 前缀过滤）、assembler（单工具失败隔离）、security（敏感键正则/DNS 注释）、result_processor（docstring）、validator（完整路径）、tool_gateway（**str** 兜底）、prompts（截断提示）、RCA（FDC 判定/空结果归因/冗余 int）、hooks（async 注释）
- [x] **取舍项保持现状**：validator schema 缓存 / 嵌套 additionalProperties / getaddrinfo 超时 / loader 重载竞态 / scan_once 线程化 / 裸 IP 拒绝（ADR 保守策略）
- [x] **文档同步**：executor / security / rca / tool_service / external + ALIGNMENT verify
- [x] **全量回归**：uv run pytest 通过

---

# 2026-08-17 工具模块重构：对齐工业级六大子组件

> 背景：工具模块原为 Facade + 5 组件（registry/executor/stats/hooks/assembler），对照工业级 Agent 工具模块存在差距（参数校验仅查未知+必填、无统一结果处理、无风险分级与审计、无选择机制）。网络调研工业级方案后，与用户「六大子组件」蓝图对比整合，确认四个方向性决策：全盘对齐六大子组件 / 安全分级+审计留痕（不拦截）/ 选择器只留接口不实现 / 引入 jsonschema。

## 任务清单（垂直切片）

- [x] **Slice 0** Facade 骨架 + 最小链路：新建 selector / validator / result_processor / security 四组件，wire 进 ToolService/executor，既有测试全绿（12 passed）
- [x] **Slice 1** validator 细化（iter_errors 全量收集 + 中文归因 + reject_unknown + 类型名映射）；base.py 委托改造；`test_tool_validator.py`（13 用例）
- [x] **Slice 2** result_processor 细化（head+tail 截断 + 错误归一化）；内置工具删内联截断 + 元数据（risk/category/concurrency_safe/max_output_length）；`test_result_processor.py` + readFile 大文件截断集成用例
- [x] **Slice 3** security 细化（RiskLevel L0-L3 + ToolAuditor 审计到日志）；executor 审计全路径接入 + per-tool 串行化锁；`test_tool_audit.py` + executor 组件测试
- [x] **Slice 4** selector 接入 get_openai_tools；修 domain/agent/executor.py:210 PEP 758 语法；`test_tool_selector.py` + `test_tool_registry_metadata.py` + `test_tool_executor_components.py` + `test_tool_hooks.py`
- [x] **Slice 5** 文档：tools.md 重写为模块接口文档 + tool_service.md/builtin.md/集成层 README 更新 + validator/result_processor/security/selector 四子文档 + ALIGNMENT + ADR×3 + issue + verify_alignment 通过

## 评审（2026-08-17）

- **全量测试 414 passed**（原 12 工具测试 + 新增 ~40 用例），无回归
- **`uv run python -m scripts.verify_alignment` 通过**（4 新组件已登记，文档死链清零）
- **Container 装配冒烟**：5 工具注册 + code_exec 正确标注 L2_DANGEROUS + 审计默认启用
- **受控审计冒烟**：search 未配置 key 优雅失败路径触发 `tool_call` 审计事件
- **新增组件**：selector（接口+全量注入）/ validator（jsonschema 严格校验）/ result_processor（head+tail 截断）/ security（分级+审计）
- **行为变更**：参数校验从「未知+必填」升级为 jsonschema 完整校验（LLM 传字符串化数字会校验失败并归因）——ADR-002 记录；结果截断从「只留前 N」升级为 head+tail（含 marker）
- **顺带修复**：domain/agent/executor.py `except json.JSONDecodeError, KeyError:` → 显式元组（PEP 758 可移植性，issue AGENT-001）；hooks.py `asyncio.iscoroutinefunction` → `inspect`（3.16 弃用告警）

### 遗留（工具模块重构）

- `app/main.py:27`、`app/integration/llm/retry.py:595` 同型 PEP 758 逗号语法，超出本次范围，仅 issue AGENT-001 记录待后续处理
- 审计密钥脱敏（params 中的 api_key 等）列为未来增强；审计默认常开（不设 settings 开关）
- 选择器向量召回（embedding 粗排 + LLM 精排）留待工具数 >50 时实现（ADR-001 记录升级路径）

---

# 2026-08-15 文档/测试以代码架构为准对齐

> 背景：代码已迁移到新分层（domain/application/integration/infrastructure/shared），
> docs 仍按旧分层（core_doc/service_doc/integration_doc/tools_doc/model_doc），tests 无对齐清单。
> 已确认方案：docs 目录镜像 + ALIGNMENT.md 映射表 + 轻量 verify_alignment.py；模块 README 暂不加。

## 任务清单

- [x] 1. 建立 `docs/ALIGNMENT.md` 映射表（代码模块 ↔ 状态 ↔ 文档 ↔ 测试）
- [x] 2. 文档目录镜像迁移
  - [x] 2a. `service_doc` → `application_doc`（session/context/task）+ `integration_doc`（llm/embedding/tools）
  - [x] 2b. `core_doc` → `domain_doc`（agent/memory/prompts/reasoning）
  - [x] 2c. `tool_doc` → `integration_doc/tools/`，`model_doc` → `infrastructure_doc/models/`
  - [x] 2d. 新建 `shared_doc/events.md`（对应 `app/shared/events.py`）
- [x] 3. 修复全部旧路径交叉链接（docs 内互链 + README/HANDOFF/AGENTS）
- [x] 4. 编写 `scripts/verify_alignment.py` + `tests/unit/test_verify_alignment.py`
- [x] 5. 运行 `uv run pytest` 全量验证
- [x] 6. 同步 README / HANDOFF / AGENTS 的旧分层描述
- [x] 7. 补齐缺失测试（test_session_manager / test_context_manager / test_container / test_settings / test_events + integration/e2e 空文件）

## 评审

### 完成情况（2026-08-15）

- 文档目录已镜像到 `app/` 顶层：`application_doc / domain_doc / integration_doc / infrastructure_doc / shared_doc / platform_doc`（`api_doc / config_doc` 保留）；`utils_doc` 已废弃迁移（logger/metrics → platform_doc/observability，error_handling/class-design → shared_doc）。
- 旧路径链接（`docs/service_doc`、`docs/core_doc`、`docs/tool_doc`、`docs/model_doc` 及 `app/services`、`app/core`、`app/models`、`app/tools`、`app_state`）全部替换为新路径。
- `docs/ALIGNMENT.md` 登记全部代码模块；`scripts/verify_alignment.py` 校验三处对齐；新增 10 个单测。
- 全量测试 `224 passed`（原 214 + 新增 10）。

### 遗留

- 空目录 `docs/service_doc`、`docs/core_doc`、`docs/tool_doc`、`docs/model_doc` 因删除被沙箱策略拦截仍存在，git 不追踪、不影响运行，待环境允许时清理。

### 第 7 项完成情况（2026-08-15）

- 新增 5 个单元测试文件，`ALIGNMENT.md` 对应条目 🔶 → ✅：
  - `test_events.py`（13 用例）· `test_settings.py`（~24 用例）· `test_context_manager.py`（10 用例）· `test_session_manager.py`（22 用例，手写 `_FakeRedis`/`_FakeDB` 按 SQLAlchemy 语句分发）· `test_container.py`（6 用例，stub Redis/引擎 + autouse 恢复全局注册表）。
- 填充两个空测试文件：`test_tool_execution.py`（6 用例，内置工具真实执行，code_exec 用 `sys.executable` 防 PATH 依赖）、`test_api.py`（5 用例，TestClient 不触发 lifespan + monkeypatch container 服务）。
- `test_memory.py` 保持空文件（目标模块 `app/domain/memory/` 全为 0 字节空壳，无可测内容，与 ⬜ 状态一致）。
- 全量测试 `325 passed`（原 224 + 新增 101）；`scripts/verify_alignment.py` 校验通过。

### 教训（2026-08-15 补充）

- `_FakeDB` 语句分发不能依赖 `column_descriptions[0]["entity"]`：聚合 select（stats/count）的 entity 也是 FROM 映射类。改用 `descs[0]["expr"]` 是否为映射类（type）区分实体行查询与函数聚合查询。
- SessionManager 构造参数 `db_session_factory` 存为 `self.db_session`（非同名属性）；fake 的 db_session 必须实现 `__aenter__/__aexit__`（`async with self.db_session() as db`）。
- 复用 fake 时要清理其状态：`list_sessions` 首页会写缓存，连续两次调用需 `fake_redis.data.clear()`，否则第二次命中缓存不查库、断言落空。
- 内置工具测试显式 `register_config(api_key="")` 重置 key，否则会读到仓库根 `.env` 的真实 TAVILY_API_KEY 触发真实网络请求。

---

# RateLimiter 审核问题修复计划

> 来源：`docs/llm/rate_limiter.md` 附录「2026-08-01 代码审核记录」6 个遗留问题。
> 方式：**逐个修复**，每修完一个停下来总结并更新文档。

---

# 结算退差 + reserve/settle（后续任务）

> 承接「工业级对比」章节的可改进点（对比 3/4），2026-08-02 已实现。

- [x] `rate_limiter.py`：TokenBucket.refund + Reservation + ReservationTokenBucket + ReservationRateLimiter（超集单类）+ Manager 单类单缓存
- [x] `llm_service.py`：迁移到 reserve/settle 统一闭环（R1/R2/R3/R8 防护：create 失败 cancel、create 成功后 settle、迭代硬取消 finally 兜底）
- [x] `test_rate_limiter.py`：新增 11 个测试（refund/Reservation/reserve），24/24 通过
- [x] `test_stream_rectify.py`：stub 适配 reserve，15/15 通过
- [x] 文档：rate_limiter.md 组件详解/调用流程/工业级对比更新

## 进度

- [x] **问题 1（严重）** 配置 0 除零崩溃 —— `TokenBucket.acquire` 对 `refill_rate <= 0` 防御，直接放行
- [x] **问题 2（中）** 持锁 sleep —— `acquire` 重构为「锁内计算 → 锁外 sleep → 循环重检」（连带解决问题 6）
- [x] **问题 3（中）** TPM 只算 prompt —— `_count_prompt_tokens` 加 `max_tokens` 输出余量
- [x] **问题 4（低）** `acquire` 返回值表述不准 —— 修正 docstring
- [x] **问题 5（低）** `async with` 用法误导 —— 移除 `__aenter__/__aexit__` 死代码 + 更新 docstring
- [x] **问题 6（低）** `_tokens` 轻微为负 —— 已由问题 2 重构连带解决（只在 `_tokens >= tokens` 时扣减）

## 评审（2026-08-02 全部完成）

6 个审核问题全部修复，测试 13/13 通过（rate_limiter）+ 37/37（stream_rectify + retry）无回归。

| 问题 | 修复方式 | 验证 |
| --- | --- | --- |
| 1 | `TokenBucket.acquire` 对 `refill_rate <= 0` 直接放行 | `test_bucket_zero_refill_disabled` |
| 2 | 锁外 sleep 循环重检 | `test_bucket_wait_does_not_block_others` / `test_bucket_cancel_does_not_corrupt_state` |
| 3 | `_count_prompt_tokens` 加 `max_tokens` 输出余量 | 37 测试无回归 |
| 4 | docstring 明确返回值语义 | 纯文档 |
| 5 | 移除 `__aenter__/__aexit__` 死代码 | `py_compile` + 全测试 |
| 6 | 由问题 2 重构连带解决 | `test_bucket_cancel_does_not_corrupt_state` 覆盖 |

## 关联文件

| 文件 | 改动 |
| --- | --- |
| `app/services/llm/rate_limiter.py` | TokenBucket.acquire / RateLimiter.acquire / docstring |
| `app/services/llm/llm_service.py` | `_count_prompt_tokens` 输出余量（问题 3） |
| `tests/unit/test_rate_limiter.py` | 新增各问题回归测试 |
| `docs/llm/rate_limiter.md` | 附录问题标记修复 + 正文已知边界同步 |

## 评审

（待各问题修复后逐条补充）
