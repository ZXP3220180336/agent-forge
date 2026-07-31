"""
LLM 子包 — 工业级 LLM 通信组件

分层架构：
    client.py        ClientManager      连接池复用与多 client 管理
    retry.py         RetryHandler      增强重试（jitter / circuit breaker / fallback）
    streaming.py     StreamParser      流式/非流式响应解析
    structured.py    StructuredOutput  结构化输出（JSON Schema）
    logger.py        LLMLogger         请求/响应日志
    rate_limiter.py  RateLimiter       客户端限流
    cost_tracker.py  CostTracker       成本计算
"""

from .client import ClientManager
from .cost_tracker import CostTracker
from .logger import LLMLogger, LLMRequestRecord
from .rate_limiter import RateLimiter
from .retry import CircuitBreaker, RetryConfig, RetryHandler
from .streaming import StreamParser
from .structured import StructuredOutput

__all__ = [
    "CircuitBreaker",
    "ClientManager",
    "CostTracker",
    "LLMLogger",
    "LLMRequestRecord",
    "RateLimiter",
    "RetryConfig",
    "RetryHandler",
    "StreamParser",
    "StructuredOutput",
]
