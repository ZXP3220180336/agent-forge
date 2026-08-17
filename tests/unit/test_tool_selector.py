"""
ToolSelector 工具选择器单元测试

覆盖：
    DefaultToolSelector 全量返回（顺序不变）
    ToolService.get_openai_tools() 默认 = 全部注册工具
    自定义过滤 selector → 只导出子集（ToolGateway 两方法签名不变，Agent 无感知）
    零注册 → 空列表
"""

from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.selector import DefaultToolSelector
from app.integration.tools.tool_service import ToolService


class _ATool(BaseTool):
    """测试工具 A。"""

    @property
    def name(self) -> str:
        return "a_tool"

    @property
    def description(self) -> str:
        return "test tool A"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="A")


class _BTool(_ATool):
    """测试工具 B。"""

    @property
    def name(self) -> str:
        return "b_tool"


class _FilterSelector:
    """只放行指定工具名的自定义选择器。"""

    def __init__(self, allowed: set[str]):
        self.allowed = allowed

    def select(self, tools: list[BaseTool]) -> list[BaseTool]:
        return [tool for tool in tools if tool.name in self.allowed]


def test_default_selector_returns_all_in_order():
    """DefaultToolSelector 全量返回（顺序不变）。"""
    tools = [_ATool(), _BTool()]
    assert DefaultToolSelector().select(tools) == tools


def test_get_openai_tools_default_returns_all():
    """默认 selector 下 get_openai_tools() = 全部注册工具。"""
    service = ToolService()
    service.register(_ATool())
    service.register(_BTool())

    names = {t["function"]["name"] for t in service.get_openai_tools()}

    assert names == {"a_tool", "b_tool"}


def test_custom_selector_filters_exported_tools():
    """自定义 selector → get_openai_tools 只导出子集。"""
    service = ToolService(selector=_FilterSelector({"a_tool"}))
    service.register(_ATool())
    service.register(_BTool())

    names = {t["function"]["name"] for t in service.get_openai_tools()}

    assert names == {"a_tool"}


def test_custom_selector_execute_unaffected():
    """选择器不影响注册表执行面（execute 仍可调全部注册工具）。"""
    service = ToolService(selector=_FilterSelector({"a_tool"}))
    service.register(_ATool())
    service.register(_BTool())

    assert service.list_tools() == ["a_tool", "b_tool"]


def test_no_tools_returns_empty():
    """零注册 → get_openai_tools() 空列表。"""
    service = ToolService()
    assert service.get_openai_tools() == []


async def test_tool_gateway_protocol_satisfied():
    """ToolGateway 协议两方法签名不变（Agent 层无感知）。"""
    from app.domain.ports.tool_gateway import ToolGateway

    service = ToolService(selector=_FilterSelector({"a_tool"}))
    service.register(_ATool())

    # 结构化类型检查：ToolService 满足 ToolGateway 协议
    assert isinstance(service, ToolGateway)
    gateway: ToolGateway = service
    assert isinstance(gateway.get_openai_tools(), list)
    result = await gateway.execute("a_tool", {})
    assert result.success is True
