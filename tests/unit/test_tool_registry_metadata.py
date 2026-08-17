"""
ToolRegistry 元数据查询单元测试

覆盖：
    all_tools / list_by_risk / list_by_category 过滤正确性
"""

from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.security import RiskLevel


class _ReadTool(BaseTool):
    """L0 只读（search 域）。"""

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "read tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "search"

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="ok")


class _WriteTool(_ReadTool):
    """L1 写（file 域）。"""

    @property
    def name(self) -> str:
        return "write"

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.L1_WRITE

    @property
    def category(self) -> str:
        return "file"


class _ExecTool(_ReadTool):
    """L2 危险（code 域）。"""

    @property
    def name(self) -> str:
        return "exec"

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.L2_DANGEROUS

    @property
    def category(self) -> str:
        return "code"


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_ReadTool())
    reg.register(_WriteTool())
    reg.register(_ExecTool())
    return reg


def test_all_tools_returns_registered_order():
    """all_tools 返回全部实例（注册顺序）。"""
    reg = _registry()
    names = [t.name for t in reg.all_tools()]
    assert names == ["read", "write", "exec"]


def test_list_by_risk_filters():
    """list_by_risk 按风险等级过滤。"""
    reg = _registry()

    l0 = [t.name for t in reg.list_by_risk(RiskLevel.L0_READONLY)]
    l1 = [t.name for t in reg.list_by_risk(RiskLevel.L1_WRITE)]
    l2 = [t.name for t in reg.list_by_risk(RiskLevel.L2_DANGEROUS)]

    assert l0 == ["read"]
    assert l1 == ["write"]
    assert l2 == ["exec"]


def test_list_by_category_filters():
    """list_by_category 按功能域过滤。"""
    reg = _registry()

    assert [t.name for t in reg.list_by_category("search")] == ["read"]
    assert [t.name for t in reg.list_by_category("file")] == ["write"]
    assert [t.name for t in reg.list_by_category("code")] == ["exec"]
    assert reg.list_by_category("none") == []
