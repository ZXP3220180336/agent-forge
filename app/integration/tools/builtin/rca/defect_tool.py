"""Wafer 缺陷查询工具（RCA：缺陷模式分析）。"""

from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.security import RiskLevel

from .data import query_defects


class QueryDefectMapTool(BaseTool):
    """查询批次 wafer 缺陷分布——确认缺陷模式与类型（证据链关键一环）。"""

    @property
    def name(self) -> str:
        return "query_defect_map"

    @property
    def description(self) -> str:
        return (
            "查询指定批次的 wafer 缺陷分布（模式 / 数量 / 主要缺陷类型）。"
            "良率异常排查中用于确认『缺陷模式（如中心聚集 / 边缘 / 随机）与主导类型』，"
            "与 FDC 参数偏离关联定位根因。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "批次号，如 LOT-A123"},
                "wafer_id": {"type": "string", "description": "wafer 号（可选），如 W-001"},
            },
            "required": ["batch_id"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        """只读查询，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "defect"

    async def execute(self, **kwargs) -> ToolResult:
        """查模拟缺陷检测数据，返回 wafer 缺陷分布。"""
        if not self.validate_parameters(**kwargs):
            return self._invalid_params_result(**kwargs)

        batch_id: str = kwargs["batch_id"]
        wafer_id = kwargs.get("wafer_id")
        records = query_defects(batch_id, wafer_id=wafer_id)

        if not records:
            scope = batch_id + (f" / {wafer_id}" if wafer_id else "")
            return ToolResult(
                success=False, content="", error=f"未找到 {scope} 的缺陷数据"
            )

        lines = [f"批次 {batch_id} wafer 缺陷分布（共 {len(records)} 片）："]
        for r in records:
            lines.append(
                f"- {r['wafer_id']}: 模式={r['pattern']}，缺陷数={r['defect_count']}，"
                f"主导类型={r['top_type']}（尺寸 {r['size_um']}um，{r['sampled_at']}）"
            )

        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={
                "source": "mock_defect",
                "batch_id": batch_id,
                # 证据链统一锚点：结果集最近时间点（最新采样，与 yield 口径一致）
                "timestamp": records[-1]["sampled_at"],
            },
        )
