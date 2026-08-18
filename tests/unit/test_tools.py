"""
ToolService 单元测试

覆盖：
    工具级并发信号量：并发执行工具数不超过 agent_max_concurrent_tools
    execute 基本流程：注册/执行/统计
    异常时信号量释放
"""

import asyncio

import pytest

from app.config import settings
from app.integration.tools.tool_service import ToolService
from app.integration.tools.base import BaseTool, ToolResult


class _SleepTool(BaseTool):
    """带延迟的测试工具，用于观测并发度。"""

    def __init__(self, delay: float = 0.05):
        self.delay = delay
        self.active = 0
        self.max_active = 0

    @property
    def name(self) -> str:
        return "sleep_tool"

    @property
    def description(self) -> str:
        return "测试用延迟工具"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return ToolResult(success=True, content="done")


@pytest.mark.asyncio
async def test_tool_service_limits_concurrency(monkeypatch):
    """工具级并发不超过 agent_max_concurrent_tools。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 2)
    reg = ToolService(max_concurrent_tools=2)
    tool = _SleepTool(delay=0.02)
    reg.register(tool)

    # 并发执行 5 次
    await asyncio.gather(*[reg.execute("sleep_tool", {}) for _ in range(5)])

    assert tool.max_active <= 2, f"并发工具数应受信号量限制（2），实际 {tool.max_active}"
    assert tool.max_active >= 1


@pytest.mark.asyncio
async def test_tool_service_execute_basic(monkeypatch):
    """execute 基本流程：成功返回 + 统计记录。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 5)
    reg = ToolService(max_concurrent_tools=5)
    tool = _SleepTool(delay=0)
    reg.register(tool)

    result = await reg.execute("sleep_tool", {})
    assert result.success
    assert result.content == "done"

    stats = reg.get_stats("sleep_tool")
    assert stats.call_count == 1
    assert stats.success_count == 1


@pytest.mark.asyncio
async def test_tool_service_releases_semaphore_on_error(monkeypatch):
    """工具异常时信号量仍释放（async with 保证）。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 2)
    reg = ToolService(max_concurrent_tools=2)

    class _FailTool(_SleepTool):
        async def execute(self, **kwargs) -> ToolResult:
            raise RuntimeError("tool boom")

    reg.register(_FailTool(delay=0))

    # 第一次异常，信号量应释放；第二次仍能进入（不阻塞）
    r1 = await reg.execute("sleep_tool", {})
    assert not r1.success
    r2 = await reg.execute("sleep_tool", {})
    assert not r2.success


class _ShutdownSpyTool(BaseTool):
    """记录 on_unload 调用次数的 spy（name 可配置以便注册多个）。"""

    def __init__(self, name: str = "shutdown_spy"):
        self._name = name
        self.unloaded = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "shutdown spy"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="ok")

    async def on_unload(self) -> None:
        self.unloaded += 1


@pytest.mark.asyncio
async def test_tool_service_shutdown_calls_on_unload():
    """shutdown 遍历全部已注册工具调用 on_unload（内置工具随应用生命周期回收）。"""
    service = ToolService()
    spy = _ShutdownSpyTool()
    service.register(spy)

    await service.shutdown()

    assert spy.unloaded == 1


@pytest.mark.asyncio
async def test_tool_service_shutdown_idempotent_and_tolerates_failure():
    """shutdown 可重复调用；单个工具 on_unload 抛异常不阻断其余。"""

    class _BoomOnUnload(_ShutdownSpyTool):
        async def on_unload(self) -> None:
            self.unloaded += 1
            raise RuntimeError("unload boom")

    service = ToolService()
    boom = _BoomOnUnload(name="boom_spy")
    good = _ShutdownSpyTool(name="good_spy")
    service.register(boom)
    service.register(good)

    await service.shutdown()  # 不抛异常（on_unload 失败被捕获）
    await service.shutdown()  # 幂等

    assert boom.unloaded == 2
    assert good.unloaded == 2
