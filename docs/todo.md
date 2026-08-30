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
