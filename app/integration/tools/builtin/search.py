"""
搜索工具 - 基于 Tavily API
"""

import asyncio
from typing import Any, ClassVar

from tavily import TavilyClient

from ..base import BaseTool, ToolResult
from ..security import RiskLevel


class SearchTool(BaseTool):
    """
    网络搜索工具
    使用 Tavily Search API 进行实时搜索
    """

    _api_key: ClassVar[str] = ""
    _search_depth: ClassVar[str] = "basic"

    def __init__(self) -> None:
        # 实例级 TavilyClient 单例（复用，api_key 变化时重建），对齐 web_browse httpx 单例
        self._client: TavilyClient | None = None
        self._client_api_key: str = ""

    def _get_client(self, api_key: str) -> TavilyClient:
        """获取 TavilyClient（实例级复用；api_key 变化时重建）。"""
        if self._client is None or self._client_api_key != api_key:
            self._client = TavilyClient(api_key=api_key)
            self._client_api_key = api_key
        return self._client

    @classmethod
    def register_config(
        cls, *, api_key: str = "", search_depth: str = "basic", **kwargs: Any
    ) -> None:
        """注入 Tavily 配置（由装配根调用，避免直接依赖 settings）。"""
        cls._api_key = api_key
        cls._search_depth = search_depth

    @property
    def risk_level(self) -> RiskLevel:
        """只读外部查询，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "search"

    @property
    def timeout(self) -> int:
        """工具自声明默认超时（秒），executor 外层超时保护。"""
        return 15

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
            return self._invalid_params_result(**kwargs)

        api_key = self._api_key

        if not api_key:
            return ToolResult(
                success=False,
                content="",
                error="未配置 TAVILY_API_KEY",
            )

        try:
            tavily = self._get_client(api_key)
            # 同步 IO 放入独立线程，不阻塞事件循环
            response = await asyncio.to_thread(
                tavily.search,
                query=kwargs["query"],
                search_depth=self._search_depth,
                include_answer=True,
            )

            # 优先返回直接答案（来源 URL 前 3 条进 metadata，供证据链回溯）
            if response.get("answer"):
                urls = [
                    r.get("url")
                    for r in response.get("results", [])[:3]
                    if r.get("url")
                ]
                return ToolResult(
                    success=True,
                    content=response["answer"],
                    metadata={"source": "tavily_answer", "urls": urls},
                )

            # 格式化搜索结果（每行追加来源 URL，证据可回溯）
            formatted_results = []
            for result in response.get("results", []):
                title = result.get("title", "无标题")
                result_content = result.get("content", "")
                url = result.get("url", "")
                url_suffix = f"（来源: {url}）" if url else ""
                formatted_results.append(f"- {title}: {result_content}{url_suffix}")

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

        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False,
                content="",
                error=f"执行 Tavily 搜索失败: {e!s}",
            )
