"""工具注册表 — 工具容器 + OpenAI 格式导出 + 元数据查询。"""

from typing import Any

from app.integration.tools.base import BaseTool
from app.integration.tools.security import RiskLevel


class ToolRegistry:
    """工具容器：注册 / 注销 / 查询 / 导出（原 ToolService 容器职责拆分）。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具，重名抛 ValueError。"""
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已存在")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """注销工具，返回是否成功。"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get(self, name: str) -> BaseTool | None:
        """获取工具实例。"""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """列出全部工具名。"""
        return list(self._tools.keys())

    def all_tools(self) -> list[BaseTool]:
        """全部工具实例（注册顺序）。"""
        return list(self._tools.values())

    def list_by_risk(self, risk_level: RiskLevel) -> list[BaseTool]:
        """按风险等级过滤工具（供安全审计 / 管理界面查询）。"""
        return [tool for tool in self._tools.values() if tool.risk_level == risk_level]

    def list_by_category(self, category: str) -> list[BaseTool]:
        """按功能域过滤工具（供按域选择 / 管理界面查询）。"""
        return [tool for tool in self._tools.values() if tool.category == category]

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """OpenAI Tool Schema 列表。"""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    def get_openai_responses(self) -> list[dict[str, Any]]:
        """OpenAI Response Schema 列表。"""
        return [tool.to_openai_response() for tool in self._tools.values()]
