"""
工具基类定义
所有工具都应继承此基类
"""

from abc import ABC, abstractmethod
from typing import Any

from app.domain.ports.tool_gateway import ToolResult


class BaseTool(ABC):
    """
    工具抽象基类

    所有工具必须实现以下方法：
    - name: 工具名称（唯一标识）
    - description: 工具描述
    - parameters: 工具参数 JSON Schema
    - execute: 工具执行逻辑
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称（唯一标识）"""

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述（用于 LLM 理解工具功能）"""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """工具参数 JSON Schema"""

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """
        执行工具逻辑

        Args:
            **kwargs: 工具参数

        Returns:
            ToolResult: 执行结果
        """

    def to_openai_tool(self) -> dict[str, Any]:
        """
        转换为 OpenAI Tool 格式

        Returns:
            OpenAI Tool Schema
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_openai_response(self) -> dict[str, Any]:
        """
        转换为 OpenAI Response 格式（用于 tool_calls 响应）

        Returns:
            OpenAI Response Schema
        """
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def validate_parameters(self, **kwargs) -> bool:
        """
        验证参数是否符合 Schema（可选实现）

        Args:
            **kwargs: 工具参数

        Returns:
            bool: 参数是否有效
        """

        # 基础验证：检查异常参数
        properties = self.parameters.get("properties", {})
        for param in kwargs:
            if param not in properties:
                return False

        # 基础验证：检查必填参数
        required = self.parameters.get("required", [])
        for param in required:
            if param not in kwargs:
                return False

        return True
