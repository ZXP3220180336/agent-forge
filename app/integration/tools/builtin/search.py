"""
搜索工具 - 基于 Tavily API
"""

import asyncio
from typing import Any, ClassVar

from tavily import TavilyClient

from ..base import BaseTool, ToolResult


class SearchTool(BaseTool):
    """
    网络搜索工具
    使用 Tavily Search API 进行实时搜索
    """

    _api_key: ClassVar[str] = ""
    _search_depth: ClassVar[str] = "basic"

    @classmethod
    def register_config(cls, *, api_key: str = "", search_depth: str = "basic", **kwargs: Any) -> None:
        """注入 Tavily 配置（由装配根调用，避免直接依赖 settings）。"""
        cls._api_key = api_key
        cls._search_depth = search_depth

    @property
    def name(self) -> str:
        return "search"

    @property
    def description(self) -> str:
        return "一个网页搜索引擎。当你需要回答关于时事、事实以及在你的知识库中找不到的信息时，应使用此工具。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "待查询内容，如北京市有哪些著名景点、上海市今年人均GDP多少",
                }
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        """
        执行搜索

        Args:
            query: 搜索关键词

        Returns:
            ToolResult: 搜索结果
        """
        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")

        api_key = self._api_key

        if not api_key:
            return ToolResult(
                success=False,
                content="",
                error="未配置 TAVILY_API_KEY",
            )

        try:
            tavily = TavilyClient(api_key=api_key)
            # 同步 IO 放入独立线程，不阻塞事件循环
            response = await asyncio.to_thread(
                tavily.search,
                query=kwargs["query"],
                search_depth=self._search_depth,
                include_answer=True,
            )

            # 优先返回直接答案
            if response.get("answer"):
                return ToolResult(
                    success=True,
                    content=response["answer"],
                    metadata={"source": "tavily_answer"},
                )

            # 格式化搜索结果
            formatted_results = []
            for result in response.get("results", []):
                formatted_results.append(f"- {result['title']}: {result['content']}")

            if not formatted_results:
                return ToolResult(
                    success=True,
                    content="抱歉，没有找到相关信息。",
                )

            content = "根据搜索，为您找到以下信息：\n" + "\n".join(formatted_results)
            return ToolResult(
                success=True,
                content=content,
                metadata={"source": "tavily_search", "count": len(formatted_results)},
            )

        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"执行 Tavily 搜索失败: {e!s}",
            )
