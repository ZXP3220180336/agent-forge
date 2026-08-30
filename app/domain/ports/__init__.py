"""领域端口层（Ports）。

领域层拥有的抽象契约，由能力层 / 基础设施层实现（依赖倒置）。
"""

from .context_budget import ContextBudgetPort
from .cost_limiter import CostLimiterPort
from .embedding_port import EmbeddingPort
from .llm_gateway import LLMGateway, StreamResult
from .token_counter import TokenCounter
from .tool_gateway import ToolGateway, ToolResult

__all__ = [
    "ContextBudgetPort",
    "CostLimiterPort",
    "EmbeddingPort",
    "LLMGateway",
    "StreamResult",
    "TokenCounter",
    "ToolGateway",
    "ToolResult",
]
