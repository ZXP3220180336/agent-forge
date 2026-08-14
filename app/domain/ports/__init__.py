"""领域端口层（Ports）。

领域层拥有的抽象契约，由能力层 / 基础设施层实现（依赖倒置）。
"""

from .llm_gateway import LLMGateway, StreamResult
from .tool_gateway import ToolGateway, ToolResult

__all__ = ["LLMGateway", "StreamResult", "ToolGateway", "ToolResult"]
