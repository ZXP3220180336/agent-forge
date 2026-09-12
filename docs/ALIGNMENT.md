# 代码模块 ↔ 文档 ↔ 测试 对齐表

> 更新日期：2026-09-12
> 原则：本表是模块状态、文档与测试路径的唯一维护登记。实现需由代码和实际测试核验，代码偏差不能自动改写已确认契约；新增/移动/删除模块或覆盖变化时同步本表和所属说明。
> 状态徽标：✅ 代码、文档、测试文件齐全 ｜ 🔶 已实现但文档或测试不全 ｜ ⬜ 空壳待实现。文件映射已在仓库核验；徽标不代表本轮运行了业务测试或逐项验证了运行契约。
> 本表维持既有路径与五列表头，所有路径相对仓库根。2026-09-12 在工作区运行全量 pytest 与 `scripts.verify_alignment`；后者检查模块登记、路径存在性、非空文件与状态要求，业务行为由测试覆盖。

| 代码模块 | 状态 | 文档 | 测试 | 说明 |
| --- | --- | --- | --- | --- |
| app/main.py | ✅ | docs/project/deployment.md | tests/e2e/test_api.py | 入口；e2e 覆盖 HTTP 层 |
| app/container.py | ✅ | docs/project/architecture.md | tests/unit/test_container.py | 装配根 |
| app/config/settings.py | ✅ | docs/config_doc/config.md | tests/unit/test_settings.py | 配置项及默认值见配置参考，不另维护数量 |
| app/api/deps.py | 🔶 | docs/api_doc/routes_doc/routes.md | (无) | DI 薄解析；经 chat_flow 间接覆盖 |
| app/api/middleware/auth.py | ⬜ | docs/api_doc/middleware_doc/middleware.md | (无) | 空文件，鉴权 mock 待实现 |
| app/api/middleware/error_handler.py | ✅ | docs/api_doc/middleware_doc/middleware.md | tests/unit/test_error_handler.py | AppError → HTTP 状态 + 统一信封（Phase D error_handler） |
| app/api/middleware/rate_limit.py | ⬜ | docs/api_doc/middleware_doc/middleware.md | (无) | 空文件待实现 |
| app/api/routes/chat.py | ✅ | docs/api_doc/routes_doc/routes.md | tests/integration/test_chat_flow.py | SSE 聊天闭环 |
| app/api/routes/session.py | 🔶 | docs/api_doc/routes_doc/routes.md | (无) | 会话 CRUD；待补路由测试 |
| app/api/routes/admin.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/routes/agent.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/routes/tool.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/schemas/request.py | ✅ | docs/api_doc/routes_doc/routes.md | tests/unit/test_request_schemas.py | 请求 DTO；请求级迭代上限边界 |
| app/api/schemas/response.py | 🔶 | docs/api_doc/routes_doc/routes.md | (无) | 响应 DTO；随路由测试覆盖 |
| app/api/schemas/agent.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现（Agent DTO） |
| app/application/context/context_manager.py | ✅ | docs/application_doc/context_doc/context.md | tests/unit/test_context_manager.py | 消息组装与 Token 截断（经 LLMGateway.count_* 计数）+ Agent 运行中上下文预算管理（ContextBudgetPort） |
| app/application/context/cost_limiter.py | ✅ | docs/application_doc/context_doc/context.md | tests/unit/test_cost_limiter.py | 成本上限判定（CostLimiterPort 实现，经 LLMGateway.calculate_cost 取成本估算） |
| app/application/session/session_manager.py | ✅ | docs/application_doc/session_doc/session.md | tests/unit/test_session_manager.py | 会话/缓存/SQL 边界按当前需求评估；缓存 None 降级见模块契约 |
| app/application/task/task_service.py | ✅ | docs/application_doc/task_doc/task.md | tests/unit/test_task_service.py | 并发闸门 |
| app/domain/agent/base.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_agent.py | Agent 基类与数据定义 |
| app/domain/agent/executor.py | ✅ | docs/domain_doc/agent_doc/executor.md | tests/unit/test_agent.py | ReActAgent 桥接（ReActStrategy 到 BaseAgent 生命周期） |
| app/domain/agent/planner.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_planner_agent.py | PlannerAgent 桥接（PlannerStrategy 到 BaseAgent 生命周期，plan/steps_executed/replan_rounds/degraded 进 metadata） |
| app/domain/agent/reflection.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_reflection_agent.py | ReflectionAgent 桥接（ReflectionStrategy 到 BaseAgent 生命周期） |
| app/domain/memory/base.py | ⬜ | docs/domain_doc/memory_doc/memory.md | tests/unit/test_memory.py | 空壳；test_memory.py 空文件 |
| app/domain/memory/long_term.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/memory_service.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/short_term.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/working.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/ports/context_budget.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议；随 ContextManager/Agent 测试覆盖 |
| app/domain/ports/cost_limiter.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议；随 CostLimiter/Agent 测试覆盖 |
| app/domain/ports/embedding_port.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议；随 EmbeddingService 测试覆盖 |
| app/domain/ports/llm_gateway.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议（含 calculate_cost / count_*）；随 Agent/LLM/成本测试覆盖 |
| app/domain/ports/tool_gateway.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议；随 Agent/工具测试覆盖 |
| app/domain/prompts/base.py | 🔶 | docs/domain_doc/prompts_doc/prompts.md | (无) | 待补测试 |
| app/domain/prompts/manager.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | PromptManager 组装入口（system/reflection/planning builder + 证据/步骤序列化） |
| app/domain/prompts/templates/planning.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | Planner 规划/重规划/汇总提示词模板 |
| app/domain/prompts/templates/system.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | SYSTEM_PROMPT 系统提示词 |
| app/domain/prompts/templates/tools.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | TOOL_FORMAT_PROMPT 工具格式提示词 |
| app/domain/prompts/templates/reflection.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_reflection.py | Reflection 自查/修正提示词模板 |
| app/domain/reasoning/_common.py | ✅ | docs/domain_doc/reasoning_doc/_common.md | tests/unit/test_reasoning_common.py | 策略共享小工具（类型化执行护栏、dispatch_error、usage 合并，三策略共用） |
| app/domain/reasoning/chain_of_thought.py | ⬜ | docs/domain_doc/reasoning_doc/reasoning.md | (无) | 空文件待实现 |
| app/domain/reasoning/planner.py | ✅ | docs/domain_doc/reasoning_doc/planner.md | tests/unit/test_planner.py | Planner 三阶段编排；STOP 直接终止，上下文超限保留 usage/plan/步骤事实且不进入同语义 fallback |
| app/domain/reasoning/react.py | ✅ | docs/domain_doc/reasoning_doc/react.md | tests/unit/test_react_strategy.py | ReAct 策略实现（ReActStrategy + ReActOutcome，含流式/非流式双通道 stream_mode；非流式另见 test_react_strategy_nonstream.py） |
| app/domain/reasoning/reflection.py | ✅ | docs/domain_doc/reasoning_doc/reflection.md | tests/unit/test_reflection.py | Reflection 自查/修正；成本与 after-turn 取消保留早退语义，strict deadline 保留稿件/critique/usage 后按超时终止 |
| app/infrastructure/database.py | ⬜ | docs/infrastructure_doc/infrastructure.md | (无) | 空文件，DB 由 container 直管 |
| app/infrastructure/redis_client.py | ⬜ | docs/infrastructure_doc/infrastructure.md | (无) | 空文件，Redis 由 container 直管 |
| app/infrastructure/models/database/base.py | 🔶 | docs/infrastructure_doc/model_doc/model.md | (无) | 共享 declarative_base |
| app/infrastructure/models/database/messages.py | 🔶 | docs/infrastructure_doc/model_doc/model.md | (无) | Message ORM |
| app/infrastructure/models/database/session.py | 🔶 | docs/infrastructure_doc/model_doc/model.md | (无) | Session ORM |
| app/infrastructure/models/database/task.py | ⬜ | docs/infrastructure_doc/model_doc/model.md | (无) | 空文件待实现 |
| app/infrastructure/models/database/tool_log.py | ⬜ | docs/infrastructure_doc/model_doc/model.md | (无) | 空文件待实现 |
| app/integration/vector_store/base.py | ⬜ | docs/integration_doc/README.md | (无) | 空文件待实现 |
| app/integration/vector_store/milvus.py | ⬜ | docs/integration_doc/README.md | (无) | 空文件待实现 |
| app/integration/embedding/embedding_service.py | 🔶 | docs/integration_doc/embedding_doc/embedding.md | tests/unit/test_embedding.py | 文本向量化（结构实现 EmbeddingPort；已实现未接线，RAG 接线待 Phase D） |
| app/integration/llm/client.py | ✅ | docs/integration_doc/llm_doc/client.md | tests/unit/test_client_manager.py | ClientManager 连接池 |
| app/integration/llm/cost_tracker.py | ✅ | docs/integration_doc/llm_doc/cost_tracker.md | tests/unit/test_cost_tracker.py | 成本追踪（CostTracker 静态定价，经 LLMService Facade 对外） |
| app/integration/llm/errors.py | ✅ | docs/integration_doc/llm_doc/error.md | tests/unit/test_errors.py | 传输错误统一处理（分类契约 ErrorCategory/ErrorClassifier + 归一/降级判定/下游决策；归一 LLMAPIError） |
| app/integration/llm/llm_service.py | ✅ | docs/integration_doc/llm_doc/llm_service.md | tests/unit/test_llm_service.py | Facade 编排；create 启动后保守结算，响应接管/usage 结算/有界观测后执行最终 Guard 并同步返回 |
| app/integration/llm/request_budget.py | ✅ | docs/integration_doc/llm_doc/request_budget.md | tests/unit/test_request_budget.py | 最终请求上下文预算闸（model_key 窗口、输出预留、tools/schema） |
| app/integration/llm/reservation_limiter.py | ✅ | docs/integration_doc/llm_doc/limiter.md | tests/unit/test_reservation_limiter.py | reserve/settle 限流 |
| app/integration/llm/retry.py | ✅ | docs/integration_doc/llm_doc/retry.md | tests/unit/test_retry.py | 熔断/重试机制（错误分类/归一迁至 llm/errors.py，经 classify_error 消费分类） |
| app/integration/llm/execution_control.py | ✅ | docs/integration_doc/llm_doc/execution_control.md | tests/unit/test_execution_control.py | 执行控制等待原语（retry/整流/llm_service 共用；reserve 排队取消/流读取竞态等调用方边界另由 test_llm_request_budget.py / test_streaming_rectifier.py 覆盖） |
| app/integration/llm/streaming.py | ✅ | docs/integration_doc/llm_doc/streaming.md | tests/unit/test_streaming.py | 流式解析 |
| app/integration/llm/streaming_rectifier.py | ✅ | docs/integration_doc/llm_doc/streaming_rectifier.md | tests/unit/test_streaming_rectifier.py | 流式整流/续接；EOF 完成态禁止重发，必要结算可见，LLM 日志失败由共享入口隔离 |
| app/integration/llm/structured.py | ✅ | docs/integration_doc/llm_doc/structure.md | tests/unit/test_generate_structured.py | 结构化三级降级 |
| app/integration/llm/token_counter.py | ✅ | docs/integration_doc/llm_doc/token_counter.md | tests/unit/test_token_counter.py | tiktoken 计数实现（get_encoder/content_to_text/TiktokenTokenCounter，经 LLMService.count_* 对外） |
| app/integration/tools/assembler.py | ✅ | docs/integration_doc/tools_doc/tool_service.md | tests/integration/test_tool_execution.py | 内置工具幂等装配 |
| app/integration/tools/base.py | ✅ | docs/integration_doc/tools_doc/tools.md | tests/unit/test_tool_validator.py | BaseTool 抽象 + 元数据（风险/分类/并发安全/超时）+ 校验委托 + 生命周期钩子 |
| app/integration/tools/executor.py | ✅ | docs/integration_doc/tools_doc/executor.md | tests/unit/test_tool_executor_components.py | 执行编排：校验归因/截断/审计/串行化/超时优先级 |
| app/integration/tools/hooks.py | ✅ | docs/integration_doc/tools_doc/tool_service.md | tests/unit/test_tool_hooks.py | 执行钩子（成功路径通知） |
| app/integration/tools/loader.py | ✅ | docs/integration_doc/tools_doc/external.md | tests/unit/test_tool_loader.py | 外部工具热加载（execute 惰性检查 + 生命周期钩子） |
| app/integration/tools/registry.py | ✅ | docs/integration_doc/tools_doc/registry.md | tests/unit/test_tool_registry_metadata.py | 注册中心 + 元数据查询（风险/分类） |
| app/integration/tools/result_processor.py | ✅ | docs/integration_doc/tools_doc/result_processor.md | tests/unit/test_result_processor.py | 结果处理器：head+tail 截断 + 错误归一化 |
| app/integration/tools/security.py | ✅ | docs/integration_doc/tools_doc/security.md | tests/unit/test_tool_audit.py | 风险分级 L0-L3 + 审计 + 审批通道（默认放行；审批测试 test_tool_approval.py） |
| app/integration/tools/selector.py | ✅ | docs/integration_doc/tools_doc/selector.md | tests/unit/test_tool_selector.py | 工具选择器（默认全量注入，预留） |
| app/integration/tools/stats.py | ✅ | docs/integration_doc/tools_doc/stats.md | tests/unit/test_tools.py | 执行统计 |
| app/integration/tools/tool_service.py | ✅ | docs/integration_doc/tools_doc/tool_service.md | tests/unit/test_tools.py | Facade 编排（六大子组件 + 外部工具热加载 + 生命周期回收） |
| app/integration/tools/validator.py | ✅ | docs/integration_doc/tools_doc/validator.md | tests/unit/test_tool_validator.py | 参数校验器：jsonschema 严格校验 + 错误归因 |
| app/integration/tools/builtin/code_exec.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置命令执行（L2 危险 + 黑名单） |
| app/integration/tools/builtin/file_ops.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置文件读写（readFile L0 / writeFile L1） |
| app/integration/tools/builtin/search.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置搜索（L0，Tavily） |
| app/integration/tools/builtin/web_browse.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置网页抓取（L0，HTML→文本 + on_unload 连接池回收） |
| app/integration/tools/external/http_api.py | ✅ | docs/integration_doc/tools_doc/external.md | tests/unit/test_http_api_tool.py | 外部工具示例：REST API 调用（生命周期钩子演示） |
| app/integration/tools/builtin/rca/data.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | RCA 模拟数据（固定可复现，LOT-A123 根因故事） |
| app/integration/tools/builtin/rca/yield_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 批次良率查询（query_batch_yield，L0） |
| app/integration/tools/builtin/rca/alerts_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 设备告警 / PM（query_equipment_alerts，L0） |
| app/integration/tools/builtin/rca/fdc_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | FDC 参数偏离（query_fdc_params，L0） |
| app/integration/tools/builtin/rca/defect_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 缺陷模式（query_defect_map，L0） |
| app/integration/tools/builtin/rca/history_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 历史案例检索（search_historical_rca，L0） |
| app/shared/events.py | ✅ | docs/shared_doc/events.md | tests/unit/test_events.py | 7 种 SSE 事件 |
| app/shared/error_handling.py | 🔶 | docs/shared_doc/error_handling.md | tests/unit/test_error_handling.py | Agent 错误处理策略（AgentErrorKind / Handler / Registry，共享内核横切） |
| app/shared/exceptions.py | ✅ | docs/shared_doc/error_handling.md | tests/unit/test_exceptions.py | 统一异常树 + AppErrorCode（含 LLMAPIError，集成层 re-export） |
| app/shared/types.py | ✅ | docs/shared_doc/types.md | tests/unit/test_types.py | 通用类型/标识（SessionId/UserId/Messages） |
| app/shared/encoding.py | ✅ | docs/shared_doc/encoding.md | tests/integration/test_tool_execution.py | 双编码解码（UTF-8 优先 + locale 回退），code_exec/readFile 复用 |
| app/platform/observability/logger.py | ✅ | docs/platform_doc/observability/logging.md | tests/unit/test_logger.py | 全局日志框架；LLM 调用事件填充与有界 best-effort 记录，日志失败不覆盖业务终态 |
| app/platform/observability/metrics.py | ⬜ | (无) | (无) | 空文件；指标规划见 architecture Phase D |
