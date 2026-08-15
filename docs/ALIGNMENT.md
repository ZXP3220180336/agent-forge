# 代码模块 ↔ 文档 ↔ 测试 对齐表

> 更新日期：2026-08-15
> 原则：**代码树是唯一事实来源**。每个代码模块在此登记状态、对应文档与测试；新增/移动/删除模块时三处同步。
> 状态徽标：✅ 代码 + 文档 + 测试齐全 ｜ 🔶 已实现但文档或测试不全 ｜ ⬜ 空壳待实现。
> 本表由 `scripts/verify_alignment.py` 校验，所有路径相对仓库根。

| 代码模块 | 状态 | 文档 | 测试 | 说明 |
| --- | --- | --- | --- | --- |
| app/main.py | ✅ | docs/deployment.md | tests/e2e/test_api.py | 入口；e2e 覆盖 HTTP 层 |
| app/container.py | ✅ | docs/architecture.md | tests/unit/test_container.py | 装配根 |
| app/config/settings.py | ✅ | docs/config_doc/config.md | tests/unit/test_settings.py | 约 90 配置项 |
| app/api/deps.py | 🔶 | docs/api_doc/api.md | (无) | DI 薄解析；经 chat_flow 间接覆盖 |
| app/api/middleware/auth.py | ⬜ | docs/api_doc/middleware_doc/middleware.md | (无) | 空文件，鉴权 mock 待实现 |
| app/api/middleware/error_handler.py | ⬜ | docs/api_doc/middleware_doc/middleware.md | (无) | 空文件待实现 |
| app/api/middleware/rate_limit.py | ⬜ | docs/api_doc/middleware_doc/middleware.md | (无) | 空文件待实现 |
| app/api/routes/chat.py | ✅ | docs/api_doc/routes_doc/routes.md | tests/integration/test_chat_flow.py | SSE 聊天闭环 |
| app/api/routes/session.py | 🔶 | docs/api_doc/routes_doc/routes.md | (无) | 会话 CRUD；待补路由测试 |
| app/api/routes/admin.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/routes/agent.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/routes/tool.py | ⬜ | docs/api_doc/routes_doc/routes.md | (无) | 空文件待实现 |
| app/api/schemas/request.py | 🔶 | docs/api_doc/api.md | (无) | 请求 DTO；随路由测试覆盖 |
| app/api/schemas/response.py | 🔶 | docs/api_doc/api.md | (无) | 响应 DTO；随路由测试覆盖 |
| app/api/schemas/agent.py | ⬜ | docs/api_doc/api.md | (无) | 空文件待实现（Agent DTO） |
| app/application/context/context_manager.py | ✅ | docs/application_doc/context_doc/context.md | tests/unit/test_context_manager.py | 消息组装与 Token 截断 |
| app/application/session/session_manager.py | ✅ | docs/application_doc/session_doc/session.md | tests/unit/test_session_manager.py | 三合一待拆分 |
| app/application/task/task_service.py | ✅ | docs/application_doc/task_doc/task.md | tests/unit/test_task_service.py | 并发闸门 |
| app/domain/agent/base.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_agent.py | Agent 基类与数据定义 |
| app/domain/agent/executor.py | ✅ | docs/domain_doc/agent_doc/agent.md | tests/unit/test_agent.py | ReAct 执行引擎 |
| app/domain/agent/planner.py | ⬜ | docs/domain_doc/agent_doc/agent.md | (无) | 空文件，PlannerAgent 待实现 |
| app/domain/agent/reasoning.py | ⬜ | docs/domain_doc/agent_doc/agent.md | (无) | 空文件待实现 |
| app/domain/memory/base.py | ⬜ | docs/domain_doc/memory_doc/memory.md | tests/unit/test_memory.py | 空壳；test_memory.py 空文件 |
| app/domain/memory/long_term.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/memory_service.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/short_term.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/memory/working.py | ⬜ | docs/domain_doc/memory_doc/memory.md | (无) | 空文件待实现 |
| app/domain/ports/llm_gateway.py | 🔶 | docs/domain_doc/README.md | (无) | 端口协议；随 Agent/LLM 测试覆盖 |
| app/domain/ports/tool_gateway.py | 🔶 | docs/domain_doc/README.md | (无) | 端口协议；随 Agent/工具测试覆盖 |
| app/domain/prompts/base.py | 🔶 | docs/domain_doc/prompts_doc/prompts.md | (无) | 待补测试 |
| app/domain/prompts/manager.py | 🔶 | docs/domain_doc/prompts_doc/prompts.md | (无) | 已实现零引用；待接线/测试 |
| app/domain/prompts/templates/planning.py | 🔶 | docs/domain_doc/prompts_doc/prompts.md | (无) | 待补测试 |
| app/domain/prompts/templates/system.py | 🔶 | docs/domain_doc/prompts_doc/prompts.md | (无) | 待补测试 |
| app/domain/prompts/templates/tools.py | 🔶 | docs/domain_doc/prompts_doc/prompts.md | (无) | 待补测试 |
| app/domain/reasoning/chain_of_thought.py | ⬜ | docs/domain_doc/reasoning_doc/reasoning.md | (无) | 空文件待实现 |
| app/domain/reasoning/react.py | ⬜ | docs/domain_doc/reasoning_doc/reasoning.md | (无) | 空文件待实现 |
| app/domain/reasoning/reflection.py | ⬜ | docs/domain_doc/reasoning_doc/reasoning.md | (无) | 空文件待实现 |
| app/infrastructure/database.py | ⬜ | docs/infrastructure_doc/infrastructure.md | (无) | 空文件，DB 由 container 直管 |
| app/infrastructure/redis_client.py | ⬜ | docs/infrastructure_doc/infrastructure.md | (无) | 空文件，Redis 由 container 直管 |
| app/infrastructure/models/database/base.py | 🔶 | docs/infrastructure_doc/model_doc/model.md | (无) | 共享 declarative_base |
| app/infrastructure/models/database/messages.py | 🔶 | docs/infrastructure_doc/model_doc/model.md | (无) | Message ORM |
| app/infrastructure/models/database/session.py | 🔶 | docs/infrastructure_doc/model_doc/model.md | (无) | Session ORM |
| app/infrastructure/models/database/task.py | ⬜ | docs/infrastructure_doc/model_doc/model.md | (无) | 空文件待实现 |
| app/infrastructure/models/database/tool_log.py | ⬜ | docs/infrastructure_doc/model_doc/model.md | (无) | 空文件待实现 |
| app/integration/vector_store/base.py | ⬜ | docs/integration_doc/README.md | (无) | 空文件待实现 |
| app/integration/vector_store/milvus.py | ⬜ | docs/integration_doc/README.md | (无) | 空文件待实现 |
| app/integration/embedding/embedding_service.py | 🔶 | docs/integration_doc/embedding_doc/embedding.md | (无) | 已实现未接线；待补测试 |
| app/integration/llm/client.py | ✅ | docs/integration_doc/llm_doc/client.md | tests/unit/test_client_manager.py | ClientManager 连接池 |
| app/integration/llm/cost_tracker.py | ✅ | docs/integration_doc/llm_doc/cost_tracker.md | tests/unit/test_cost_tracker.py | 成本追踪 |
| app/integration/llm/llm_service.py | 🔶 | docs/integration_doc/llm_doc/llm.md | (无) | Facade；经各子模块测试覆盖，待补专属测试 |
| app/integration/llm/reservation_limiter.py | ✅ | docs/integration_doc/llm_doc/limiter.md | tests/unit/test_reservation_limiter.py | reserve/settle 限流 |
| app/integration/llm/retry.py | ✅ | docs/integration_doc/llm_doc/retry.md | tests/unit/test_retry.py | 熔断/重试/错误分类 |
| app/integration/llm/streaming.py | ✅ | docs/integration_doc/llm_doc/streaming.md | tests/unit/test_streaming.py | 流式解析 |
| app/integration/llm/streaming_rectifier.py | ✅ | docs/integration_doc/llm_doc/streaming_rectifier.md | tests/unit/test_streaming_rectifier.py | 流式整流 |
| app/integration/llm/structured.py | ✅ | docs/integration_doc/llm_doc/structure.md | tests/unit/test_generate_structured.py | 结构化三级降级 |
| app/integration/tools/assembler.py | 🔶 | docs/integration_doc/tool_service_doc/tool_service.md | tests/unit/test_tools.py | 内置工具装配；组件级测试待补 |
| app/integration/tools/base.py | 🔶 | docs/integration_doc/tool_service_doc/tool_service.md | tests/unit/test_tools.py | BaseTool 抽象；组件级测试待补 |
| app/integration/tools/executor.py | 🔶 | docs/integration_doc/tool_service_doc/tool_service.md | tests/unit/test_tools.py | 执行器；重试/超时/校验路径测试待补 |
| app/integration/tools/hooks.py | 🔶 | docs/integration_doc/tool_service_doc/tool_service.md | tests/unit/test_tools.py | 执行钩子；组件级测试待补 |
| app/integration/tools/registry.py | 🔶 | docs/integration_doc/tool_service_doc/tool_service.md | tests/unit/test_tools.py | 工具注册中心；组件级测试待补 |
| app/integration/tools/stats.py | 🔶 | docs/integration_doc/tool_service_doc/tool_service.md | tests/unit/test_tools.py | 工具统计；组件级测试待补 |
| app/integration/tools/tool_service.py | 🔶 | docs/integration_doc/tool_service_doc/tool_service.md | tests/unit/test_tools.py | Facade；test_tools.py 仅覆盖并发/基本执行/异常释放 |
| app/integration/tools/builtin/code_exec.py | 🔶 | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/unit/test_tools.py | 内置代码执行；测试待补 |
| app/integration/tools/builtin/file_ops.py | 🔶 | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/unit/test_tools.py | 内置文件读写；测试待补 |
| app/integration/tools/builtin/search.py | 🔶 | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/unit/test_tools.py | 内置搜索；测试待补 |
| app/integration/tools/builtin/web_browse.py | 🔶 | docs/integration_doc/tools_doc/builtin_doc/builtin.md | tests/unit/test_tools.py | 内置网页抓取；测试待补 |
| app/shared/events.py | ✅ | docs/shared_doc/events.md | tests/unit/test_events.py | 7 种 SSE 事件 |
| app/utils/exceptions.py | ⬜ | docs/utils_doc/error_handling.md | (无) | 空文件待实现 |
| app/utils/helpers.py | ⬜ | docs/utils_doc/class-design.md | (无) | 空文件待实现 |
| app/utils/logger.py | ✅ | docs/utils_doc/logging.md | tests/unit/test_logger.py | 全局日志框架 |
| app/utils/metrics.py | ⬜ | (无) | (无) | 空文件；指标规划见 architecture Phase D |
