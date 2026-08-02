"""
ToolRegistry 单元测试

覆盖：
    工具级并发信号量：并发执行工具数不超过 agent_max_concurrent_tools
    execute 基本流程：注册/执行/统计
    异常时信号量释放
"""

import asyncio

import pytest

from app.config import settings
from app.tools.base import BaseTool, ToolResult
from app.tools.registry import ToolRegistry


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
async def test_tool_registry_limits_concurrency(monkeypatch):
    """工具级并发不超过 agent_max_concurrent_tools。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 2)
    reg = ToolRegistry()
    tool = _SleepTool(delay=0.02)
    reg.register(tool)

    # 并发执行 5 次
    await asyncio.gather(*[reg.execute("sleep_tool", {}) for _ in range(5)])

    assert tool.max_active <= 2, f"并发工具数应受信号量限制（2），实际 {tool.max_active}"
    assert tool.max_active >= 1


@pytest.mark.asyncio
async def test_tool_registry_execute_basic(monkeypatch):
    """execute 基本流程：成功返回 + 统计记录。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 5)
    reg = ToolRegistry()
    tool = _SleepTool(delay=0)
    reg.register(tool)

    result = await reg.execute("sleep_tool", {})
    assert result.success
    assert result.content == "done"

    stats = reg.get_stats("sleep_tool")
    assert stats.call_count == 1
    assert stats.success_count == 1


@pytest.mark.asyncio
async def test_tool_registry_releases_semaphore_on_error(monkeypatch):
    """工具异常时信号量仍释放（async with 保证）。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 2)
    reg = ToolRegistry()

    class _FailTool(_SleepTool):
        async def execute(self, **kwargs) -> ToolResult:
            raise RuntimeError("tool boom")

    reg.register(_FailTool(delay=0))

    # 第一次异常，信号量应释放；第二次仍能进入（不阻塞）
    r1 = await reg.execute("sleep_tool", {})
    assert not r1.success
    r2 = await reg.execute("sleep_tool", {})
    assert not r2.success
