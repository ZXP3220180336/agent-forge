"""良率查询工具（RCA：批次良率）。"""

from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.security import RiskLevel

from .data import query_yield


class QueryBatchYieldTool(BaseTool):
    """查询指定批次良率记录（按工艺 step），识别骤降——良率异常排查第一步。"""

    @property
    def name(self) -> str:
        return "query_batch_yield"

    @property
    def description(self) -> str:
        return (
            "查询指定批次的良率记录（按工艺 step），识别良率骤降。"
            "良率工程师排查批次异常时使用——先确认『哪个 step 骤降、涉及哪台设备』。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "批次号，如 LOT-A123"},
                "time_range": {
                    "type": "string",
                    "description": "时间窗口（可选），如 2026-08-12 08:00~2026-08-12 20:00",
                },
            },
            "required": ["batch_id"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        """只读查询，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "yield"

    async def execute(self, **kwargs) -> ToolResult:
        """查模拟 YMS 数据，返回按 step 的良率记录（含骤降标记）。"""
        if not self.validate_parameters(**kwargs):
            return self._invalid_params_result(**kwargs)

        batch_id: str = kwargs["batch_id"]
        time_range = kwargs.get("time_range")
        records = query_yield(batch_id, time_range=time_range)
        if not records:
            if time_range:
                # 区分「窗口内无记录」与「批次不存在」：窗口过滤不应把前者归因为后者
                return ToolResult(
                    success=False,
                    content="",
                    error=f"批次 '{batch_id}' 在指定时间窗口内无良率记录",
                )
            return ToolResult(
                success=False, content="", error=f"未找到批次 '{batch_id}' 的良率记录"
            )

        lines = [f"批次 {batch_id} 良率记录（按 step）："]
        for r in records:
            mark = " ⚠ 骤降" if r["drop"] else ""
            lines.append(
                f"- step={r['step']}: {r['yield_rate']}%"
                f"（equipment={r['equipment']}，{r['timestamp']}）{mark}"
            )

        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={
                "source": "mock_yms",
                "batch_id": batch_id,
                # 证据链统一锚点：结果集最近时间点（最新记录，max 取时间最大，与数据顺序无关）
                "timestamp": max(records, key=lambda r: r["timestamp"])["timestamp"],
            },
        )
