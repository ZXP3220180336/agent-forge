# 代码模块 ↔ 文档 ↔ 测试 对齐表

> 更新日期：2026-09-20
> 原则：本表是模块状态、文档与测试路径的唯一维护登记。实现需由代码和实际测试核验，代码偏差不能自动改写已确认契约；新增/移动/删除模块或覆盖变化时同步本表和所属说明。
> 状态徽标：✅ 代码、文档、测试文件齐全 ｜ 🔶 已实现但文档或测试不全 ｜ ⬜ 空壳待实现。文件映射已在仓库核验；徽标不代表本轮运行了业务测试或逐项验证了运行契约。
> 本表维持既有路径与五列表头，所有路径相对仓库根。2026-09-13 在工作区运行全量 pytest 与 `scripts.verify_alignment`；后者检查模块登记、路径存在性、非空文件与状态要求，业务行为由测试覆盖。

| 代码模块 | 状态 | 文档 | 测试 | 说明 |
| --- | --- | --- | --- | --- |
| app/main.py | ✅ | docs/project/deployment.md | tests/e2e/test_api.py | 入口；e2e 覆盖 HTTP 层 |
| app/container.py | ✅ | docs/project/architecture.md | tests/unit/test_container.py | 装配根 |
| app/config/settings.py | ✅ | docs/config_doc/config.md | tests/unit/test_settings.py | DB-F01 有限数据库预算、池容量与方言校验；专项 tests/unit/test_database_settings.py 覆盖环境注入/脱敏；DB-F02 独立运行时消费部分预算，Container/Store/迁移待后续接线 |
| app/api/deps.py | 🔶 | docs/api_doc/routes_doc/routes.md | tests/e2e/test_api.py | DI 薄解析；聊天路由只获取已装配 ChatService |
| app/api/middleware/auth.py | ⬜ | docs/api_doc/middleware_doc/middleware.md | (无) | 空文件，鉴权 mock 待实现 |
| app/api/middleware/error_handler.py | ✅ | docs/api_doc/middleware_doc/middleware.md | tests/unit/test_error_handler.py | AppError → HTTP 状态 + 统一信封（Phase D error_handler） |
| app/api/middleware/rate_limit.py | ⬜ | docs/api_doc/middleware_doc/middleware.md | (无) | 空文件待实现 |
| app/api/routes/chat.py | ✅ | docs/api_doc/routes_doc/routes.md | tests/integration/test_chat_flow.py | HTTP/SSE 与断连适配；专用响应边界在 ASGI 发送失败时关闭运行 |
| app/api/routes/session.py | 🔶 | docs/api_doc/routes_doc/routes.md | (无) | 会话 CRUD；待补路由测试 |
| app/api/routes/admin.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/routes/agent.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/routes/tool.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/schemas/request.py | ✅ | docs/api_doc/routes_doc/routes.md | tests/unit/test_request_schemas.py | 请求 DTO；请求级迭代上限边界 |
| app/api/schemas/response.py | 🔶 | docs/api_doc/routes_doc/routes.md | (无) | 响应 DTO；随路由测试覆盖 |
| app/api/schemas/agent.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现（Agent DTO） |
| app/application/chat/chat_service.py | ✅ | docs/application_doc/chat_doc/chat.md | tests/unit/test_chat_service.py | 聊天预检、消息快照、run/Agent Owner、停止与结果提交 |
| app/application/context/context_manager.py | ✅ | docs/application_doc/context_doc/context.md | tests/unit/test_context_manager.py | 消息组装与 Token 截断；以当前消息 ID 固定历史快照边界 |
| app/application/context/cost_limiter.py | ✅ | docs/application_doc/context_doc/context.md | tests/unit/test_cost_limiter.py | 成本上限判定（CostLimiterPort 实现，经 LLMGateway.calculate_cost 取成本估算） |
| app/application/session/session_manager.py | ✅ | docs/application_doc/session_doc/session.md | tests/unit/test_session_manager.py | 会话/缓存/SQL；消息主键与 `before_message_id` 快照查询契约 |
| app/application/task/task_service.py | ✅ | docs/application_doc/task_doc/task.md | tests/unit/test_task_service.py | Agent 并发闸门；run 级取消索引；显式关闭 Agent 子流 |
| app/domain/agent/base.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_agent.py | Agent 运行身份、实例并发拒绝与结果生命周期；策略跨层继承另见 test_tool_lifecycle_wiring.py |
| app/domain/agent/executor.py | ✅ | docs/domain_doc/agent_doc/executor.md | tests/unit/test_agent.py | ReActAgent 桥接（ReActStrategy 到 BaseAgent 生命周期） |
| app/domain/agent/planner.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_planner_agent.py | PlannerAgent 桥接（PlannerStrategy 到 BaseAgent 生命周期，plan/steps_executed/replan_rounds/degraded 进 metadata） |
| app/domain/agent/reflection.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_reflection_agent.py | ReflectionAgent 桥接（ReflectionStrategy 到 BaseAgent 生命周期） |
| app/domain/memory/base.py | ⬜ | docs/domain_doc/memory_doc/memory.md | tests/unit/test_memory.py | 空壳；test_memory.py 空文件 |
| app/domain/memory/long_term.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/memory_service.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/short_term.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/working.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/ports/context_budget.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议：count_tokens + trim_messages；随 ContextManager/Agent 测试覆盖 |
| app/domain/ports/cost_limiter.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议；随 CostLimiter/Agent 测试覆盖 |
| app/domain/ports/embedding_port.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议；随 EmbeddingService 测试覆盖 |
| app/domain/ports/llm_gateway.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议（含 calculate_cost / count_* / 流式业务取消类型化出口）；随 Agent/LLM/成本测试覆盖 |
| app/domain/ports/tool_gateway.py | 🔶 | docs/domain_doc/ports_doc/ports.md | (无) | 端口协议；随 Agent/工具测试覆盖 |
| app/domain/ports/tool_execution.py | ✅ | docs/domain_doc/ports_doc/ports.md | tests/unit/test_tool_lifecycle_contract.py | 工具运行身份、控制信号、事实状态与同步 sink 契约 |
| app/domain/prompts/base.py | 🔶 | docs/domain_doc/prompts_doc/prompts.md | (无) | 待补测试 |
| app/domain/prompts/manager.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | PromptManager 公开组装入口与固定模板开销 |
| app/domain/prompts/_reflection_payload.py | ✅ | docs/domain_doc/prompts_doc/reflection_payload.md | tests/unit/test_prompts.py | Reflection evidence/draft/issues 字段级语义缩减；包内纯函数组件 |
| app/domain/prompts/_planner_payload.py | ✅ | docs/domain_doc/prompts_doc/planner_payload.md | tests/unit/test_prompts.py | Planner plan/replan/summarize 分层语义投影；包内纯函数组件 |
| app/domain/prompts/templates/planning.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | Planner 规划/重规划/汇总提示词模板 |
| app/domain/prompts/templates/system.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | SYSTEM_PROMPT 系统提示词 |
| app/domain/prompts/templates/tools.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_prompts.py | TOOL_FORMAT_PROMPT 工具格式提示词 |
| app/domain/prompts/templates/reflection.py | ✅ | docs/domain_doc/prompts_doc/prompts.md | tests/unit/test_reflection.py | Reflection 自查/修正提示词模板 |
| app/domain/reasoning/_common.py | ✅ | docs/domain_doc/reasoning_doc/_common.md | tests/unit/test_reasoning_common.py | 策略共享小工具与实例并发执行拒绝装饰器；实例拒绝另经 test_tool_lifecycle_wiring.py 覆盖 |
| app/domain/reasoning/chain_of_thought.py | ⬜ | docs/domain_doc/reasoning_doc/reasoning.md | (无) | 空文件待实现 |
| app/domain/reasoning/execution.py | ✅ | docs/domain_doc/reasoning_doc/reasoning.md | tests/unit/test_reasoning_execution.py | 三策略共享的六类不可变执行参数值对象；策略专属恢复预算以 None 表示不适用 |
| app/domain/reasoning/planner.py | ✅ | docs/domain_doc/reasoning_doc/planner.md | tests/unit/test_planner.py | Planner 三阶段编排与语义预算接线；上下文超限保留 usage/plan/步骤事实且不进入同语义 fallback |
| app/domain/reasoning/_planner_steps.py | ✅ | docs/domain_doc/reasoning_doc/planner.md | tests/unit/test_planner.py | Planner 步骤规范化、公开计划快照和单步审计记录纯转换；不接管 replan/usage/终态 |
| app/domain/reasoning/reflection.py | ✅ | docs/domain_doc/reasoning_doc/reflection.md | tests/unit/test_reflection.py | Reflection 自查/修正；语义缩减只影响 prompt 视图；Guard 终止保留最近稿、原始证据、critique 与 usage |
| app/domain/reasoning/react.py | ✅ | docs/domain_doc/reasoning_doc/react.md | tests/unit/test_react_strategy.py | ReAct 策略实现；批次部分成果、完整协议回执、类型化控制传播与真实线程路径（经 handle.run_sync 的完整批次）另经 test_tool_lifecycle_wiring.py 覆盖；流式/非流式双通道 |
| app/domain/reasoning/_react_protocol.py | ✅ | docs/domain_doc/reasoning_doc/react.md | tests/unit/test_react_protocol.py | ReAct final_answer、调用身份与动作指纹纯协议转换；不接管协议预算、历史或工具执行 |
| app/domain/reasoning/tool_batch.py | ✅ | docs/domain_doc/reasoning_doc/tool_batch.md | tests/unit/test_tool_lifecycle_contract.py | Collector 负责 revision 去重、深快照与关闭；Runner 负责并行结果、控制时兄弟有界收尾；跨层接管及协议配对经 test_tool_lifecycle_wiring.py 覆盖 |
| app/infrastructure/database.py | ✅ | docs/infrastructure_doc/database_doc/database.md | tests/unit/test_database.py | DB-F02 独立生命周期、受控工厂、只读探测、有界清理与日志脱敏；实例日志脱敏入口 `configure_database_logging` 供迁移命令复用；tests/unit/test_database_lifecycle_review.py 覆盖真实 Session Owner；schema 校验、Container 接线和真实 PostgreSQL 验收未完成 |
| app/infrastructure/database_migrations.py | ✅ | docs/infrastructure_doc/database_doc/migrations.md | tests/unit/test_database_migrations.py | DB-F03a/b 文件/全历史校验与整批执行、离线命令 Owner；事务/取消/提交未知另见 test_database_command.py；版本表结构/首迁移/基线及真实原子性验收待后续片 |
| scripts/migrate.py | 🔶 | docs/project/deployment.md | tests/unit/test_database_cli.py | DB-F03b 唯一 CLI，UTF-8/脱敏/配置消费与预算映射；真实 spawn 监督见 test_database_cli_supervision.py；F04 gate 缺失时零连接拒绝升级 |
| scripts/init_db.py | ✅ | docs/project/deployment.md | tests/unit/test_database_cli.py | 仅委托 scripts.migrate.main，无第二套建表机制 |
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
| app/integration/llm/llm_service.py | ✅ | docs/integration_doc/llm_doc/llm_service.md | tests/unit/test_llm_service.py | Facade 编排；委托请求计划，保留响应接管/usage 结算/有界观测、最终 Guard 与公开异常翻译 |
| app/integration/llm/request_execution.py | ✅ | docs/integration_doc/llm_doc/request_execution.md | tests/unit/test_llm_request_budget.py | R2 请求计划与真实 create 边界；预算、预留、执行控制及调用阶段结算，Facade 接线另由 test_llm_service.py 覆盖 |
| app/integration/llm/request_budget.py | ✅ | docs/integration_doc/llm_doc/request_budget.md | tests/unit/test_request_budget.py | 最终请求上下文预算闸（model_key 窗口、输出预留、tools/schema） |
| app/integration/llm/reservation_limiter.py | ✅ | docs/integration_doc/llm_doc/limiter.md | tests/unit/test_reservation_limiter.py | reserve/settle 限流 |
| app/integration/llm/retry.py | ✅ | docs/integration_doc/llm_doc/retry.md | tests/unit/test_retry.py | 熔断/重试机制（错误分类/归一迁至 llm/errors.py，经 classify_error 消费分类） |
| app/integration/llm/execution_control.py | ✅ | docs/integration_doc/llm_doc/execution_control.md | tests/unit/test_execution_control.py | 执行控制等待原语（retry/整流/request_execution 共用；reserve 排队取消/流读取竞态等调用方边界另由 test_llm_request_budget.py / test_streaming_rectifier.py 覆盖） |
| app/integration/llm/streaming.py | ✅ | docs/integration_doc/llm_doc/streaming.md | tests/unit/test_streaming.py | 流式解析 |
| app/integration/llm/stream_consumption.py | ✅ | docs/integration_doc/llm_doc/stream_consumption.md | tests/unit/test_stream_consumption.py | R3 单流读取、结果累积、终止竞争、接缝处理与提前关闭；不接管 Reservation 结算 |
| app/integration/llm/streaming_rectifier.py | ✅ | docs/integration_doc/llm_doc/streaming_rectifier.md | tests/unit/test_streaming_rectifier.py | 流式整流/续接；业务取消先收尾再类型化冒泡且不生成取消 SSE；EOF 完成态禁止重发，必要结算可见 |
| app/integration/llm/structured.py | ✅ | docs/integration_doc/llm_doc/structure.md | tests/unit/test_generate_structured.py | 结构化三级降级 |
| app/integration/llm/structured_codec.py | ✅ | docs/integration_doc/llm_doc/structure.md | tests/unit/test_generate_structured.py | R1 纯转换与校验；公开调用测试覆盖双副本与回喂边界 |
| app/integration/llm/token_counter.py | ✅ | docs/integration_doc/llm_doc/token_counter.md | tests/unit/test_token_counter.py | tiktoken 计数实现（get_encoder/content_to_text/TiktokenTokenCounter，经 LLMService.count_* 对外） |
| app/integration/tools/assembler.py | ✅ | docs/integration_doc/tools_doc/tool_service.md | tests/integration/test_tool_execution.py | 内置工具幂等装配 |
| app/integration/tools/admission.py | ✅ | docs/integration_doc/tools_doc/admission.md | tests/unit/test_tool_admission.py | 全局/单运行共享准入、有界排队、轮转公平、可中断等待与撤回/关闭转换 |
| app/integration/tools/base.py | ✅ | docs/integration_doc/tools_doc/tools.md | tests/unit/test_tool_executor_components.py | BaseTool 抽象 + 元数据 + 保守安全重试声明 + 校验委托 + 生命周期钩子 |
| app/integration/tools/execution.py | ✅ | docs/integration_doc/tools_doc/execution.md | tests/unit/test_tool_execution_supervisor.py | 进程内真实句柄、有界清理与迟回值接管；启用门禁见 test_tool_enablement.py；Executor跨层验证见 test_tool_attempt_lifecycle.py，持久恢复未实现 |
| app/integration/tools/executor.py | ✅ | docs/integration_doc/tools_doc/executor.md | tests/unit/test_tool_executor_components.py | 执行编排：安全重试、控制复查及 Integration 优先事实接管；生命周期契约另见 test_tool_lifecycle_contract.py |
| app/integration/tools/hooks.py | ✅ | docs/integration_doc/tools_doc/tool_service.md | tests/unit/test_tool_hooks.py | 成功路径有界通知 + 参数/结果快照隔离；Executor 组合边界另见 test_tool_executor_components.py |
| app/integration/tools/loader.py | ✅ | docs/integration_doc/tools_doc/external.md | tests/unit/test_tool_loader.py | 外部工具热加载（execute 惰性检查 + 生命周期钩子） |
| app/integration/tools/registry.py | ✅ | docs/integration_doc/tools_doc/registry.md | tests/unit/test_tool_registry_metadata.py | 注册中心（定义预检）+ 元数据查询（风险/分类） |
| app/integration/tools/result_processor.py | ✅ | docs/integration_doc/tools_doc/result_processor.md | tests/unit/test_result_processor.py | 结果处理器：head+tail 截断 + 错误归一化 |
| app/integration/tools/security.py | ✅ | docs/integration_doc/tools_doc/security.md | tests/unit/test_tool_audit.py | 风险分级 L0-L3 + 审计 + 审批通道（默认放行；审批测试 test_tool_approval.py） |
| app/integration/tools/selector.py | ✅ | docs/integration_doc/tools_doc/selector.md | tests/unit/test_tool_selector.py | 工具选择器（默认全量注入，预留） |
| app/integration/tools/stats.py | ✅ | docs/integration_doc/tools_doc/stats.md | tests/unit/test_tools.py | 执行统计 |
| app/integration/tools/tool_service.py | ✅ | docs/integration_doc/tools_doc/tool_service.md | tests/unit/test_tools.py | Facade 强制透传 call/facts，模型仅导出获准工具；契约另经 test_tool_lifecycle_contract.py 覆盖；外部工具热加载与生命周期回收 |
| app/integration/tools/validator.py | ✅ | docs/integration_doc/tools_doc/validator.md | tests/unit/test_tool_validator.py | 参数校验器：jsonschema 严格校验 + 错误归因 |
| app/integration/tools/builtin/code_exec.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置命令适配器（L2 危险 + 黑名单）；B 未就绪，正式执行/导出关闭 |
| app/integration/tools/builtin/file_ops.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置文件读写；只读适配器受控执行见 test_tool_readonly_execution.py，写适配器线程登记见 test_tool_write_execution.py，writeFile 正式准入等待 B |
| app/integration/tools/builtin/search.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置搜索（L0，Tavily） |
| app/integration/tools/builtin/web_browse.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/integration/test_tool_execution.py | 内置网页抓取（L0，HTML→文本 + on_unload 连接池回收） |
| app/integration/tools/external/http_api.py | ✅ | docs/integration_doc/tools_doc/external.md | tests/unit/test_http_api_tool.py | 外部工具示例：REST API 调用（生命周期钩子演示） |
| app/integration/tools/builtin/rca/data.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | RCA 模拟数据（固定可复现，LOT-A123 根因故事） |
| app/integration/tools/builtin/rca/yield_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 批次良率查询（query_batch_yield，L0） |
| app/integration/tools/builtin/rca/alerts_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 设备告警 / PM（query_equipment_alerts，L0） |
| app/integration/tools/builtin/rca/fdc_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | FDC 参数偏离（query_fdc_params，L0） |
| app/integration/tools/builtin/rca/defect_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 缺陷模式（query_defect_map，L0） |
| app/integration/tools/builtin/rca/history_tool.py | ✅ | docs/integration_doc/tools_doc/builtin_doc/rca.md | tests/unit/test_rca_tools.py | 历史案例检索（search_historical_rca，L0） |
| app/shared/json_schema.py | ✅ | docs/shared_doc/json_schema.md | tests/unit/test_json_schema.py | 固定 2020-12；根/子声明和本地引用预检；LLM、ReAct、工具复用 |
| app/shared/events.py | ✅ | docs/shared_doc/events.md | tests/unit/test_events.py | 7 种 SSE 事件 |
| app/shared/error_handling.py | 🔶 | docs/shared_doc/error_handling.md | tests/unit/test_error_handling.py | Agent 错误处理策略（AgentErrorKind / Handler / Registry，共享内核横切） |
| app/shared/exceptions.py | ✅ | docs/shared_doc/error_handling.md | tests/unit/test_exceptions.py | 统一异常树 + AppErrorCode；工具控制类型另经 test_tool_lifecycle_contract.py 覆盖 |
| app/shared/types.py | ✅ | docs/shared_doc/types.md | tests/unit/test_types.py | 通用类型/标识（SessionId/UserId/Messages） |
| app/shared/encoding.py | ✅ | docs/shared_doc/encoding.md | tests/integration/test_tool_execution.py | 双编码解码（UTF-8 优先 + locale 回退），code_exec/readFile 复用 |
| app/shared/observation.py | ✅ | docs/shared_doc/observation.md | tests/unit/test_observation.py | 非关键观测异常隔离（G0-6）：终态、请求与收尾路径 8 处复用；只隔离不限时 |
| app/platform/observability/logger.py | ✅ | docs/platform_doc/observability/logging.md | tests/unit/test_logger.py | 全局日志框架；LLM 调用事件填充与有界 best-effort 记录，日志失败不覆盖业务终态 |
| app/platform/observability/metrics.py | ⬜ | (无) | (无) | 空文件；指标规划见 architecture Phase D |
