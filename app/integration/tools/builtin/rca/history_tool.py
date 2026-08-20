"""历史 RCA 案例检索工具（良率根因分析：历史经验复用）。"""

from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.security import RiskLevel

from .data import search_history


class SearchHistoricalRcaTool(BaseTool):
    """检索历史 RCA 案例——用过往『症状 → 根因』经验佐证当前排查结论。"""

    @property
    def name(self) -> str:
        return "search_historical_rca"

    @property
    def description(self) -> str:
        return (
            "检索历史良率根因分析（RCA）案例，返回匹配的『症状 / 根因 / 证据 / 解决方式』。"
            "良率异常排查中用于复用过往经验、佐证当前根因结论。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "症状描述，如『etch 良率骤降 压力偏离』",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "返回案例数（默认 3）",
                },
            },
            "required": ["query"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        """只读查询，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "history"

    async def execute(self, **kwargs) -> ToolResult:
        """关键词匹配历史案例（RAG embedding 召回为后续增强）。"""
        if not self.validate_parameters(**kwargs):
            return self._invalid_params_result(**kwargs)

        query: str = kwargs["query"]
        top_k: int = kwargs.get("top_k", 3)  # jsonschema 已保证 integer（1-5）
        cases = search_history(query, top_k=top_k)

        if not cases:
            return ToolResult(
                success=False,
                content="",
                error=f"未检索到与『{query}』相关的历史案例（证据不足，建议补充数据）",
            )

        lines = [f"命中 {len(cases)} 条历史案例（按相关度）："]
        for c in cases:
            conf = int(c["confidence"] * 100)
            lines.extend(
                [
                    f"- {c['case_id']}（{c['timestamp']}）[相关度 {conf}%]",
                    f"  症状: {c['symptom']}",
                    f"  根因: {c['root_cause']}",
                    f"  证据: {c['evidence']}",
                    f"  解决: {c['resolution']}",
                ]
            )

        top_confidence = max(c["confidence"] for c in cases)
        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={
                "source": "mock_history",
                "query": query,
                "top_k": top_k,
                "top_confidence": top_confidence,
            },
        )
