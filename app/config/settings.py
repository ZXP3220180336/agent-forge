"""
配置类定义
使用 Pydantic Settings 从环境变量加载配置

所有配置项集中管理，支持 .env 文件和系统环境变量。
配置分组清晰，便于维护和扩展。

配置分组：
- 应用配置：APP_NAME, APP_VERSION, DEBUG
- API 配置：API_PREFIX, CORS_ORIGINS
- LLM 配置：LLM_API_KEY, LLM_MODEL_ID, ...
- 上下文配置：MAX_CONTEXT_TOKENS, ...
- Agent 配置：AGENT_MAX_ITERATIONS, AGENT_TIMEOUT, ...
- 记忆配置：MEMORY_ENABLED, MEMORY_MAX_SHORT_TERM, ...
- 数据库配置：DATABASE_URL, DATABASE_POOL_SIZE, ...
- Redis 配置：REDIS_URL, REDIS_SESSION_TTL
- 工具配置：TOOL_TIMEOUT, TOOL_MAX_RETRIES
- Tavily 配置：TAVILY_API_KEY, ...
- 日志配置：LOG_LEVEL, LOG_FORMAT, ...
- 监控配置：METRICS_ENABLED, METRICS_PORT
- 安全配置：JWT_SECRET_KEY, JWT_ALGORITHM, ...

注意：
- 环境变量命名使用全大写 + 下划线（如 LLM_API_KEY）
- 敏感信息（如 API Key）必须通过环境变量配置，不要硬编码
- 生产环境务必修改 JWT_SECRET_KEY
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    应用配置类

    所有配置项从环境变量加载，支持 .env 文件。
    使用 Pydantic 进行类型验证和自动转换。

    示例：
        >>> from app.config import settings
        >>> settings.llm_model_id
        'gpt-4'
        >>> settings.llm_config
        {'api_key': 'sk-xxx', 'base_url': '...', 'model': 'gpt-4', ...}
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="allow",
    )

    # ===== 应用配置 =====
    app_name: str = "AI Agent System"
    app_version: str = "1.0.0"
    debug: bool = False

    # ===== API 配置 =====
    api_prefix: str = "/api"
    cors_origins: list[str] = ["*"]

    # ===== LLM 配置 =====
    # 主模型（用于主要对话）
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model_id: str = "gpt-4"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    llm_timeout: int = 60

    # 推理模型（用于深度思考，如 DeepSeek-R1）
    llm_reasoning_model_id: str = ""  # 空则使用主模型
    llm_reasoning_temperature: float = 0.7
    llm_reasoning_max_tokens: int = 8192

    # 快速模型（用于简单任务，如分类、提取）
    llm_fast_model_id: str = ""  # 空则使用主模型
    llm_fast_temperature: float = 0.0
    llm_fast_max_tokens: int = 2048

    # 嵌入模型（用于向量化）
    llm_embedding_model_id: str = "text-embedding-3-small"
    llm_embedding_dimensions: int = 1536

    # LLM 高级配置（重试、熔断、限流）
    llm_max_retries: int = 2
    llm_stream_max_retries: int = 1  # 流式整流重试次数（首 token 前中断才整流；0=禁用）
    llm_base_delay: float = 1.0
    llm_max_delay: float = 30.0
    llm_use_jitter: bool = True
    # 熔断：滑动时间窗口 + 错误率判定（参考 Hystrix 模型）
    llm_circuit_window_seconds: float = 10.0  # 滑动时间窗口长度（秒）
    llm_circuit_error_threshold: float = 0.5  # 窗口内错误率熔断阈值（50%）
    llm_circuit_request_volume_threshold: int = (
        20  # 窗口内最小请求量，不足则不做错误率评估
    )
    llm_circuit_all_failed_min: int = 3  # 低流量纯失败保护：全部失败且达此样本量才熔断
    llm_circuit_recovery_timeout: float = 30.0
    llm_circuit_half_open_max_requests: int = 3
    llm_fallback_model_id: str = ""  # 主模型降级备用
    llm_proxy_url: str = ""
    llm_main_rpm: int = 60
    llm_reasoning_rpm: int = 30
    llm_fast_rpm: int = 100
    # TPM（Tokens Per Minute）—— 与 RPM 组成双桶限流。默认参考 DeepSeek 官方限额
    llm_main_tpm: int = 2_000_000
    llm_reasoning_tpm: int = 2_000_000
    llm_fast_tpm: int = 2_000_000

    # ===== 上下文配置 =====
    max_context_tokens: int = 128000
    max_output_tokens: int = 4096
    max_history_rounds: int = 20

    # ===== Agent 配置 =====
    agent_max_iterations: int = 10
    agent_timeout: int = 300  # 5分钟
    agent_streaming: bool = True

    # 任务优先级配置
    agent_priority_levels: list[Literal["low", "normal", "high", "urgent"]] = [
        "low",
        "normal",
        "high",
        "urgent",
    ]
    agent_default_priority: Literal["low", "normal", "high", "urgent"] = "normal"
    agent_high_priority_timeout: int = 600  # 高优先级任务超时时间（10分钟）
    agent_low_priority_timeout: int = 180  # 低优先级任务超时时间（3分钟）
    agent_priority_queue_size: int = 100  # 优先级队列大小

    # 并发控制配置
    agent_max_concurrent_tasks: int = 10  # 最大并发任务数
    agent_max_concurrent_tools: int = 3  # 单个任务最大并发工具数
    agent_task_queue_size: int = 50  # 任务队列大小
    agent_worker_pool_size: int = 5  # 工作线程池大小

    # ===== 记忆配置 =====
    memory_enabled: bool = False
    memory_max_short_term: int = 10  # 短期记忆条数
    memory_vector_db: Literal["milvus", "qdrant", "pinecone"] = "milvus"
    memory_collection: str = "agent_memory"

    # ===== 数据库配置 =====
    database_url: str = "postgresql+asyncpg://user:pass@localhost/db"
    database_pool_size: int = 20
    database_max_overflow: int = 10
    database_echo: bool = False

    # ===== Redis 配置 =====
    redis_url: str = "redis://localhost:6379/0"
    redis_session_ttl: int = 604800  # 7天

    # ===== 工具配置 =====
    tool_timeout: int = 30  # 工具执行超时（秒）
    tool_max_retries: int = 3  # 工具执行最大重试次数
    tool_max_output_length: int = 100_000  # 工具输出最大字符数（code_exec、readFile）
    tool_max_content_length: int = 50_000  # 网页抓取最大字符数（web_browse）

    # ===== Tavily 配置 =====
    tavily_api_key: str = ""
    tavily_search_depth: Literal["basic", "advanced"] = "basic"

    # ===== 日志配置 =====
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_file: str = "logs/app.log"

    # ===== 监控配置 =====
    metrics_enabled: bool = False
    metrics_port: int = 9090

    # ===== 安全配置 =====
    jwt_secret_key: str = "your-secret-key-change-in-production"
    jwt_algorithm: Literal["HS256", "HS384", "HS512", "RS256"] = "HS256"
    jwt_expire_minutes: int = 1440  # 24小时

    # ===== 字段验证器 =====

    @field_validator("llm_temperature")
    @classmethod
    def validate_temperature(cls, v: float) -> float:
        """验证温度范围（0-2）"""
        if not 0 <= v <= 2:
            raise ValueError(f"温度必须在 0-2 之间，当前值: {v}")
        return v

    @field_validator("agent_max_iterations")
    @classmethod
    def validate_max_iterations(cls, v: int) -> int:
        """验证迭代次数（1-100）"""
        if v < 1 or v > 100:
            raise ValueError(f"迭代次数必须在 1-100 之间，当前值: {v}")
        return v

    @field_validator("agent_max_concurrent_tasks")
    @classmethod
    def validate_concurrent_tasks(cls, v: int) -> int:
        """验证并发任务数（1-100）"""
        if v < 1 or v > 100:
            raise ValueError(f"并发任务数必须在 1-100 之间，当前值: {v}")
        return v

    @field_validator("agent_priority_queue_size")
    @classmethod
    def validate_queue_size(cls, v: int) -> int:
        """验证队列大小（1-10000）"""
        if v < 1 or v > 10000:
            raise ValueError(f"队列大小必须在 1-10000 之间，当前值: {v}")
        return v

    @field_validator("llm_embedding_dimensions")
    @classmethod
    def validate_embedding_dimensions(cls, v: int) -> int:
        """验证嵌入维度必须是正数"""
        if v <= 0:
            raise ValueError(f"嵌入维度必须为正数，当前值: {v}")
        return v

    @field_validator("jwt_expire_minutes")
    @classmethod
    def validate_jwt_expire(cls, v: int) -> int:
        """验证 JWT 过期时间（1-10080 分钟，即 1分钟-7天）"""
        if v < 1 or v > 10080:
            raise ValueError(f"JWT 过期时间必须在 1-10080 分钟之间，当前值: {v}")
        return v

    @property
    def is_production(self) -> bool:
        """是否为生产环境"""
        return not self.debug

    @property
    def llm_config(self) -> dict:
        """获取主模型配置字典"""
        return {
            "api_key": self.llm_api_key,
            "base_url": self.llm_base_url,
            "model": self.llm_model_id,
            "temperature": self.llm_temperature,
            "max_tokens": self.llm_max_tokens,
            "timeout": self.llm_timeout,
        }

    @property
    def llm_reasoning_config(self) -> dict:
        """获取推理模型配置字典"""
        model_id = self.llm_reasoning_model_id or self.llm_model_id
        return {
            "api_key": self.llm_api_key,
            "base_url": self.llm_base_url,
            "model": model_id,
            "temperature": self.llm_reasoning_temperature,
            "max_tokens": self.llm_reasoning_max_tokens,
            "timeout": self.llm_timeout,
        }

    @property
    def llm_fast_config(self) -> dict:
        """获取快速模型配置字典"""
        model_id = self.llm_fast_model_id or self.llm_model_id
        return {
            "api_key": self.llm_api_key,
            "base_url": self.llm_base_url,
            "model": model_id,
            "temperature": self.llm_fast_temperature,
            "max_tokens": self.llm_fast_max_tokens,
            "timeout": self.llm_timeout,
        }

    @property
    def llm_embedding_config(self) -> dict:
        """获取嵌入模型配置字典"""
        return {
            "api_key": self.llm_api_key,
            "base_url": self.llm_base_url,
            "model": self.llm_embedding_model_id,
            "dimensions": self.llm_embedding_dimensions,
        }

    @property
    def agent_config(self) -> dict:
        """获取 Agent 配置字典"""
        return {
            "max_iterations": self.agent_max_iterations,
            "timeout": self.agent_timeout,
            "streaming": self.agent_streaming,
            "priority_levels": self.agent_priority_levels,
            "default_priority": self.agent_default_priority,
            "high_priority_timeout": self.agent_high_priority_timeout,
            "low_priority_timeout": self.agent_low_priority_timeout,
            "priority_queue_size": self.agent_priority_queue_size,
        }

    @property
    def concurrency_config(self) -> dict:
        """获取并发控制配置字典"""
        return {
            "max_concurrent_tasks": self.agent_max_concurrent_tasks,
            "max_concurrent_tools": self.agent_max_concurrent_tools,
            "task_queue_size": self.agent_task_queue_size,
            "worker_pool_size": self.agent_worker_pool_size,
        }

    @property
    def database_config(self) -> dict:
        """获取数据库配置字典"""
        return {
            "url": self.database_url,
            "pool_size": self.database_pool_size,
            "max_overflow": self.database_max_overflow,
            "echo": self.database_echo,
        }

    @property
    def redis_config(self) -> dict:
        """获取 Redis 配置字典"""
        return {
            "url": self.redis_url,
            "session_ttl": self.redis_session_ttl,
        }

    @property
    def memory_config(self) -> dict:
        """获取记忆系统配置字典"""
        return {
            "enabled": self.memory_enabled,
            "max_short_term": self.memory_max_short_term,
            "vector_db": self.memory_vector_db,
            "collection": self.memory_collection,
        }

    @property
    def tool_config(self) -> dict:
        """获取工具配置字典"""
        return {
            "timeout": self.tool_timeout,
            "max_retries": self.tool_max_retries,
            "max_output_length": self.tool_max_output_length,
            "max_content_length": self.tool_max_content_length,
        }


@lru_cache
def get_settings() -> Settings:
    """
    获取配置实例（单例模式）
    使用 lru_cache 确保只创建一次
    """
    return Settings()


# 全局配置实例
settings = get_settings()
