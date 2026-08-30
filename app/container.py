# ============================================
# container.py - 装配根（Composition Root）
# ============================================

import asyncio

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.context.context_manager import ContextManager
from app.application.context.cost_limiter import CostLimiter
from app.application.session.session_manager import SessionManager
from app.application.task.task_service import TaskService
from app.integration.embedding import EmbeddingService
from app.integration.llm.client import ClientManager
from app.integration.llm.llm_service import LLMService
from app.integration.llm.reservation_limiter import (
    ReservationLimiterConfig,
    ReservationLimiterManager,
)
from app.integration.llm.retry import (
    CircuitBreakerConfig,
    RetryConfig,
    RetryHandlerManager,
)
from app.integration.llm.streaming_rectifier import StreamingRectifier
from app.integration.llm.structured import StructuredOutput
from app.integration.tools.builtin import (
    CodeExecTool,
    ReadFileTool,
    SearchTool,
    WebBrowseTool,
    WriteFileTool,
)
from app.integration.tools.tool_service import ToolService

from .config import settings
from .platform.observability.logger import get_logger, setup_logging

logger = get_logger("container")


class Container:
    """
    应用全局状态容器。

    持有所有共享服务实例的单例引用，在应用启动时初始化，在应用关闭时清理。
    使用类而非模块级变量，是为了支持类型提示和 IDE 自动补全。
    """

    def __init__(self):
        # 服务实例（启动时初始化，运行中保持）
        self.redis: Redis | None = None
        self.db_session_factory: async_sessionmaker[AsyncSession] | None = None
        # 持有显式的 engine 引用，以便在 shutdown 时正确释放
        self._engine: AsyncEngine | None = None

        # 管理器实例（依赖上面两个基础设施）
        self.session_manager: SessionManager | None = None
        self.context_manager: ContextManager | None = None
        self.llm_service: LLMService | None = None
        self.tool_service: ToolService | None = None
        self.task_service: TaskService | None = None
        self.embedding_service: EmbeddingService | None = None
        # 成本上限判定器（无状态纯函数，可共享单例；agent_max_cost 未配置时为 None=不启用）
        self.cost_limiter: CostLimiter | None = None
        # Agent 运行参数（initialize 时从 settings 填充，供 chat 路由构造 AgentContext）
        self.agent_params: dict = {}
        # 记录初始化状态
        self.initialized = False
        self._errors: list[str] = []

    async def initialize(self):
        """
        初始化所有服务实例。
        在 FastAPI 应用启动时调用（lifespan 事件）。

        单个基础设施初始化失败不影响整体启动，但会记录警告。
        """
        # 0. 配置日志框架（装配根唯一读 settings，在此下发日志配置）
        setup_logging(
            level=settings.log_level,
            log_file=settings.log_file,
            log_format=settings.log_format,
        )

        # 1. 创建 Redis 连接
        try:
            self.redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            await self.redis.ping()
            logger.info("Redis 连接成功")
        except Exception as e:  # noqa: BLE001
            self._errors.append(f"Redis 连接失败: {e}")
            logger.warning("Redis 不可用（服务降级）: %s", e)
            self.redis = None

        # 2. 创建数据库连接池
        try:
            engine = create_async_engine(
                settings.database_url,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                pool_pre_ping=True,
            )
            # engine 创建本身不触发连接，先保留引用
            self._engine = engine
            self.db_session_factory = async_sessionmaker(
                engine,
                expire_on_commit=False,
            )
            logger.info("数据库引擎创建成功")
        except Exception as e:  # noqa: BLE001
            self._errors.append(f"数据库初始化失败: {e}")
            logger.warning("数据库不可用（服务降级）: %s", e)
            self._engine = None
            self.db_session_factory = None

        # 3. 创建管理器实例
        self.session_manager = SessionManager(
            redis_client=self.redis,
            db_session_factory=self.db_session_factory,
        )

        # 3. 注册 LLM 客户端配置 & 创建服务
        # ClientManager 管理连接池，三种模型按需获取
        ClientManager.register_config(
            "main",
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model_id,
            proxy_url=settings.llm_proxy_url or None,
        )

        reasoning_model = settings.llm_reasoning_model_id or settings.llm_model_id
        ClientManager.register_config(
            "reasoning",
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=reasoning_model,
        )

        fast_model = settings.llm_fast_model_id or settings.llm_model_id
        ClientManager.register_config(
            "fast",
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=fast_model,
        )

        # 3.5 注入 LLM 可靠性配置（重试/熔断/限流）——子模块不直接依赖 settings

        RetryHandlerManager.register_config(
            config=RetryConfig(
                max_retries=settings.llm_max_retries,
                base_delay=settings.llm_base_delay,
                max_delay=settings.llm_max_delay,
                use_jitter=settings.llm_use_jitter,
            ),
            circuit_breaker_config=CircuitBreakerConfig(
                window_seconds=settings.llm_circuit_window_seconds,
                error_threshold=settings.llm_circuit_error_threshold,
                request_volume_threshold=settings.llm_circuit_request_volume_threshold,
                all_failed_min=settings.llm_circuit_all_failed_min,
                recovery_timeout=settings.llm_circuit_recovery_timeout,
                half_open_max_requests=settings.llm_circuit_half_open_max_requests,
            ),
        )

        def _reservation_config(
            key: str, quantile_field: str
        ) -> ReservationLimiterConfig:
            return ReservationLimiterConfig(
                rpm=getattr(settings, f"llm_{key}_rpm", 60),
                tpm=getattr(settings, f"llm_{key}_tpm", 2_000_000),
                quantile=getattr(settings, quantile_field, 0.95),
                safety_margin=getattr(settings, "llm_reserve_safety_margin", 1.15),
                min_samples=getattr(settings, "llm_reserve_min_samples", 30),
                window=getattr(settings, "llm_reserve_window", 256),
            )

        ReservationLimiterManager.register_config(
            {
                "main": _reservation_config("main", "llm_reserve_quantile"),
                "reasoning": _reservation_config(
                    "reasoning", "llm_reserve_reasoning_quantile"
                ),
                "fast": _reservation_config("fast", "llm_reserve_quantile"),
            }
        )

        # 结构化输出默认预算（extract 未显式传 max_tokens 时用）
        StructuredOutput.register_config(settings.llm_structured_max_tokens)

        # 流式整流退避配置（StreamingRectifier 不直接依赖 settings）
        StreamingRectifier.register_config(
            base_delay=settings.llm_base_delay,
            max_delay=settings.llm_max_delay,
            use_jitter=settings.llm_use_jitter,
        )

        # LLM 运行期配置注入（fallback / 自适应预留 / 流式整流次数）
        LLMService.register_config(
            fallback_model_id=settings.llm_fallback_model_id,
            adaptive_reserve=settings.llm_adaptive_reserve,
            stream_max_retries=settings.llm_stream_max_retries,
        )
        self.llm_service = LLMService()  # 空构造，通过 ClientManager 获取 client

        # ContextManager 依赖 LLMService（token 计数经 LLMGateway 端口），须在其后构造
        self.context_manager = ContextManager(
            session_manager=self.session_manager,
            llm=self.llm_service,
            max_context_tokens=settings.max_context_tokens,
            max_output_tokens=settings.max_output_tokens,
        )

        # 4. 内置工具配置注入（register_config，随后空构造装配）
        SearchTool.register_config(
            api_key=settings.tavily_api_key,
            search_depth=settings.tavily_search_depth,
        )
        WebBrowseTool.register_config(
            max_content_length=settings.tool_max_content_length
        )
        CodeExecTool.register_config(max_output_length=settings.tool_max_output_length)
        ReadFileTool.register_config(
            max_output_length=settings.tool_max_output_length,
            allowed_dirs=settings.tool_allowed_dirs,
        )
        WriteFileTool.register_config(allowed_dirs=settings.tool_allowed_dirs)

        # 注册内置工具到全局注册中心
        self.tool_service = ToolService(
            max_concurrent_tools=settings.agent_max_concurrent_tools,
            tool_timeout=settings.tool_timeout,
            tool_max_retries=settings.tool_max_retries,
            external_config_source=lambda key: getattr(settings, key, None),
        )
        try:
            registered = self.tool_service.init_default_tools()
            logger.info("已注册工具: %s", registered)
        except Exception as e:  # noqa: BLE001
            self._errors.append(f"工具初始化失败: {e}")
            logger.warning("工具初始化失败（服务降级）: %s", e)

        # 冷启动即扫描外部工具：get_openai_tools() 对 LLM 可见（execute 惰性检查仅覆盖运行期增量）
        try:
            await self.tool_service.refresh_external_tools()
        except Exception as e:  # noqa: BLE001
            logger.warning("外部工具冷启动扫描失败（不影响启动）: %s", e)

        # 5. 任务调度服务（并发 Agent 任务信号量）
        self.task_service = TaskService(
            max_concurrent=settings.agent_max_concurrent_tasks
        )

        # 6. EmbeddingService
        self.embedding_service = EmbeddingService(
            client=ClientManager.get_client("main"),
            model=settings.llm_embedding_model_id,
            dimensions=settings.llm_embedding_dimensions,
        )

        # Agent 运行参数（装配根读 settings 后下发，供 chat 路由构造 AgentContext）
        self.agent_params = {
            "max_iterations": settings.agent_max_iterations,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            # 启用总时间上限：agent_timeout 原为「从未使用的默认任务超时」，接为 ReAct 循环总时长护栏
            "max_execution_time": settings.agent_timeout,
            # 上下文预算：轮次（agent_max_context_rounds）+ token（复用全局 max_context_tokens）
            "max_context_rounds": settings.agent_max_context_rounds,
            "max_context_tokens": settings.max_context_tokens,
        }

        # 成本上限：agent_max_cost 未配置（None）或 LLM 服务降级 → 不注入，ReAct 循环零开销。
        # CostLimiter 经 LLMGateway 端口取成本估算（calculate_cost，LLM 模块 Facade 实现），
        # 应用层不直接依赖集成层子组件
        self.cost_limiter = (
            CostLimiter(
                ceiling=settings.agent_max_cost,
                llm=self.llm_service,
                model=settings.llm_model_id,
            )
            if settings.agent_max_cost is not None and self.llm_service is not None
            else None
        )

        self.initialized = True
        logger.info("应用初始化完成")

    async def shutdown(self):
        """
        清理所有资源。
        在 FastAPI 应用关闭时调用（lifespan 事件）。
        """
        # 0. 工具资源回收须在基础设施（redis/engine/LLM）关闭前——on_unload 可能依赖它们
        if self.tool_service is not None:
            await self.tool_service.shutdown()

        cleanup_tasks = []

        if self.redis:
            cleanup_tasks.append(self.redis.close())

        if self._engine is not None:
            cleanup_tasks.append(self._engine.dispose())

        # 关闭 LLM 客户端连接池（AsyncOpenAI 底层 httpx 连接池），优雅退出
        cleanup_tasks.append(ClientManager.close_all())

        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        self.initialized = False
        logger.info("应用已关闭")


# 模块级单例
container = Container()
