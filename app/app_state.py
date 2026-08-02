# ============================================
# app_state.py - 应用状态管理模块
# ============================================

import asyncio

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings
from .services import (
    ContextManager,
    EmbeddingService,
    LLMService,
    SessionManager,
    TaskService,
    ToolService,
)


class AppState:
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
        # 记录初始化状态
        self.initialized = False
        self._errors: list[str] = []

    async def initialize(self):
        """
        初始化所有服务实例。
        在 FastAPI 应用启动时调用（lifespan 事件）。

        单个基础设施初始化失败不影响整体启动，但会记录警告。
        """
        # 1. 创建 Redis 连接
        try:
            self.redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            await self.redis.ping()
            print("  [OK] Redis 连接成功")
        except Exception as e:
            self._errors.append(f"Redis 连接失败: {e}")
            print(f"  [WARN] Redis 不可用（服务降级）: {e}")
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
            print("  [OK] 数据库引擎创建成功")
        except Exception as e:
            self._errors.append(f"数据库初始化失败: {e}")
            print(f"  [WARN] 数据库不可用（服务降级）: {e}")
            self._engine = None
            self.db_session_factory = None

        # 3. 创建管理器实例
        self.session_manager = SessionManager(
            redis_client=self.redis,
            db_session_factory=self.db_session_factory,
        )

        self.context_manager = ContextManager(
            session_manager=self.session_manager,
            model_name=settings.llm_model_id,
            max_context_tokens=settings.max_context_tokens,
            max_output_tokens=settings.max_output_tokens,
        )

        # 3. 注册 LLM 客户端配置 & 创建服务
        # ClientManager 管理连接池，三种模型按需获取
        from app.services.llm import ClientManager

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

        self.llm_service = LLMService()  # 空构造，通过 ClientManager 获取 client

        # 4. 注册内置工具到全局注册中心
        self.tool_service = ToolService()
        try:
            registered = self.tool_service.init_default_tools()
            print(f"  [OK] 已注册工具: {registered}")
        except Exception as e:
            self._errors.append(f"工具初始化失败: {e}")
            print(f"  [WARN] 工具初始化失败（服务降级）: {e}")

        # 5. 任务调度服务（并发 Agent 任务信号量）
        self.task_service = TaskService()

        # 6. EmbeddingService
        self.embedding_service = EmbeddingService(
            client=ClientManager.get_client("main"),
            model=settings.llm_embedding_model_id,
            dimensions=settings.llm_embedding_dimensions,
        )

        self.initialized = True
        print("  应用初始化完成")

    async def shutdown(self):
        """
        清理所有资源。
        在 FastAPI 应用关闭时调用（lifespan 事件）。
        """
        cleanup_tasks = []

        if self.redis:
            cleanup_tasks.append(self.redis.close())

        if self._engine is not None:
            cleanup_tasks.append(self._engine.dispose())

        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        self.initialized = False
        print("  应用已关闭")


# 模块级单例
app_state = AppState()
