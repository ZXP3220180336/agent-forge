"""FDC 工艺参数查询工具（RCA：参数偏离检测）。"""

from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.security import RiskLevel

from .data import query_fdc


class QueryFdcParamsTool(BaseTool):
    """查询设备 FDC 工艺参数偏离——确认工艺窗口是否越界。"""

    @property
    def name(self) -> str:
        return "query_fdc_params"

    @property
    def description(self) -> str:
        return (
            "查询指定设备的 FDC 工艺参数及其偏离情况（value / baseline / 偏离百分比 / 状态）。"
            "良率异常排查中用于确认『设备工艺参数是否越出窗口』。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "equipment_id": {
                    "type": "string",
                    "description": "设备号，如 ETCH-01",
                },
                "process_step": {
                    "type": "string",
                    "description": "工艺 step（可选），如 ETCH",
                },
                "time_range": {
                    "type": "string",
                    "description": "时间窗口（可选），如 2026-08-12 08:00~2026-08-12 20:00（看偏离随时间发展）",
                },
            },
            "required": ["equipment_id"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        """只读查询，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "fdc"

    async def execute(self, **kwargs) -> ToolResult:
        """查模拟 FDC 数据，返回参数偏离列表（偏离项重点标注）。"""
        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")

        equipment_id: str = kwargs["equipment_id"]
        process_step = kwargs.get("process_step")
        time_range = kwargs.get("time_range")
        records = query_fdc(equipment_id, process_step=process_step, time_range=time_range)

        if not records:
            scope = f"{equipment_id}" + (f" / {process_step}" if process_step else "")
            return ToolResult(
                success=False, content="", error=f"未找到 {scope} 的 FDC 参数记录"
            )

        lines = [f"{equipment_id} FDC 工艺参数（共 {len(records)} 项）："]
        for r in records:
            mark = " ⚠ 偏离" if r["status"] == "deviated" else ""
            lines.append(
                f"- {r['parameter']} [{r['unit']}]: value={r['value']}，"
                f"baseline={r['baseline']}，偏离 {r['deviation_pct']}%（{r['timestamp']}）{mark}"
            )

        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={
                "source": "mock_fdc",
                "equipment_id": equipment_id,
                "timestamp": records[0]["timestamp"],
            },
        )
