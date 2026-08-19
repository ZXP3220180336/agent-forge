"""设备告警查询工具（RCA：设备告警 / PM）。"""

from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.security import RiskLevel

from .data import query_alerts


class QueryEquipmentAlertsTool(BaseTool):
    """查询设备告警 / PM 记录——定位机台异常 / 维护历史。"""

    @property
    def name(self) -> str:
        return "query_equipment_alerts"

    @property
    def description(self) -> str:
        return (
            "查询指定设备的告警 / 预防性维护（PM）记录。"
            "良率异常排查中用于确认『涉及机台是否有异常告警或临近维护』。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "equipment_id": {
                    "type": "string",
                    "description": "设备号，如 ETCH-01（不填查全部）",
                },
                "alert_type": {
                    "type": "string",
                    "enum": ["ALARM", "PM", "INFO"],
                    "description": "告警类型（可选）：ALARM 异常 / PM 维护 / INFO 常规",
                },
                "time_range": {
                    "type": "string",
                    "description": "时间窗口（可选），如 2026-08-12 08:00~2026-08-12 20:00",
                },
            },
            "required": [],
        }

    @property
    def risk_level(self) -> RiskLevel:
        """只读查询，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "equipment"

    async def execute(self, **kwargs) -> ToolResult:
        """查模拟 MES 数据，返回告警 / PM 列表。"""
        if not self.validate_parameters(**kwargs):
            return self._invalid_params_result(**kwargs)

        equipment_id = kwargs.get("equipment_id")
        alert_type = kwargs.get("alert_type")
        time_range = kwargs.get("time_range")
        records = query_alerts(
            equipment_id=equipment_id,
            alert_type=alert_type,
            time_range=time_range,
        )

        if not records:
            scope = equipment_id or "全部设备"
            return ToolResult(
                success=False,
                content="",
                error=f"未找到 {scope} 的告警 / PM 记录",
            )

        lines = [f"设备告警 / PM 记录（共 {len(records)} 条）："]
        for r in records:
            lines.append(
                f"- [{r['alert_id']}] [{r['alert_type']}/{r['severity']}] {r['equipment_id']}: "
                f"{r['message']}（{r['timestamp']}）"
            )

        return ToolResult(
            success=True,
            content="\n".join(lines),
            metadata={
                "source": "mock_mes",
                "equipment_id": equipment_id or "all",
                "timestamp": records[0]["timestamp"],
            },
        )
