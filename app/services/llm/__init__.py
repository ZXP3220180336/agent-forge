"""
LLM 子包 — 工业级 LLM 通信组件

分层架构：
    client.py        ClientManager      连接池复用与多 client 管理
    retry.py         RetryHandler      增强重试（jitter / circuit breaker / fallback）
    streaming.py     StreamParser      流式/非流式响应解析
    streaming_rectifier.py StreamingRectifier 流式整流重试策略
    structured.py    StructuredOutput  结构化输出（JSON Schema）
    reservation_limiter.py ReservationLimiter 客户端限流（reserve/settle 形态）
    cost_tracker.py       CostTracker       成本计算
"""

from .client import ClientManager
from .cost_tracker import CostTracker
from .reservation_limiter import ReservationLimiterConfig, ReservationLimiterManager
from .retry import (
    CircuitBreaker,
    CircuitBreakerConfig,
    RetryConfig,
    RetryHandler,
    RetryHandlerManager,
)
from .streaming import StreamParser
from .streaming_rectifier import StreamingRectifier
from .structured import StructuredOutput

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "ClientManager",
    "CostTracker",
    "ReservationLimiterConfig",
    "ReservationLimiterManager",
    "RetryConfig",
    "RetryHandler",
    "RetryHandlerManager",
    "StreamParser",
    "StreamingRectifier",
    "StructuredOutput",
]
