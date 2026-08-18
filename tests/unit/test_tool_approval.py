"""
工具人工审批通道单元测试

覆盖：
    AutoApprovalGate 默认放行（requires_approval=True 也能执行）
    自定义拒绝 gate → requires_approval 工具被拦截（不执行 + 返回失败 + 审计）
    requires_approval=False 工具不触发 gate
"""

import pytest

from app.domain.ports.tool_gateway import ErrorCode
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.security import AutoApprovalGate
from app.integration.tools.tool_service import ToolService


class _ApprovalTool(BaseTool):
    """可配置 requires_approval 的测试工具，记录是否真实执行。"""

    def __init__(self, *, requires_approval: bool = True):
        self._approval = requires_approval
        self.executed = False

    @property
    def name(self) -> str:
        return "approval_tool"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def requires_approval(self) -> bool:
        return self._approval

    async def execute(self, **kwargs) -> ToolResult:
        self.executed = True
        return ToolResult(success=True, content="done")


class _RecordingGate:
    """记录审批请求并按配置放行 / 拒绝。"""

    def __init__(self, allow: bool):
        self.allow = allow
        self.requests: list[tuple[str, dict]] = []

    async def request(self, tool_name: str, parameters: dict) -> bool:
        self.requests.append((tool_name, parameters))
        return self.allow


def test_auto_gate_instantiable():
    """AutoApprovalGate 可实例化（默认审批通道）。"""
    assert AutoApprovalGate() is not None


@pytest.mark.asyncio
async def test_auto_gate_executes_approval_tool():
    """默认审批通道下 requires_approval=True 工具正常执行。"""
    tool = _ApprovalTool(requires_approval=True)
    service = ToolService()
    service.register(tool)

    result = await service.execute("approval_tool", {})

    assert result.success is True
    assert tool.executed is True


@pytest.mark.asyncio
async def test_rejecting_gate_blocks_tool_execution():
    """拒绝 gate → requires_approval 工具被拦截：不执行 + 返回失败。"""
    gate = _RecordingGate(allow=False)
    tool = _ApprovalTool(requires_approval=True)
    service = ToolService(approval_gate=gate)
    service.register(tool)

    result = await service.execute("approval_tool", {})

    assert result.success is False
    assert "人工审批" in result.error
    assert result.error_code == ErrorCode.REJECTED
    assert tool.executed is False  # 工具未被真实执行
    assert gate.requests == [("approval_tool", {})]  # gate 收到正确参数


@pytest.mark.asyncio
async def test_gate_not_called_when_not_requires_approval():
    """requires_approval=False 工具不触发 gate（即使 gate 会拒绝）。"""
    gate = _RecordingGate(allow=False)
    tool = _ApprovalTool(requires_approval=False)
    service = ToolService(approval_gate=gate)
    service.register(tool)

    result = await service.execute("approval_tool", {})

    assert result.success is True
    assert tool.executed is True
    assert gate.requests == []  # gate 未被调用


@pytest.mark.asyncio
async def test_rejecting_gate_records_audit():
    """审批拒绝路径同样审计留痕（覆盖全路径审计）。"""
    from app.integration.tools.security import ToolAuditor

    class _SpyAuditor(ToolAuditor):
        def __init__(self):
            super().__init__(enabled=True)
            self.records = []

        async def record(self, **kwargs):
            self.records.append(kwargs)

    spy = _SpyAuditor()
    service = ToolService(approval_gate=_RecordingGate(allow=False), auditor=spy)
    service.register(_ApprovalTool(requires_approval=True))

    await service.execute("approval_tool", {})

    assert len(spy.records) == 1
    assert spy.records[0]["success"] is False
    assert "人工审批" in spy.records[0]["error"]
