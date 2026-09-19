"""
ToolService 单元测试

覆盖：
    共享准入：并发执行工具数不超过单运行/全局工具上限
    execute 基本流程：注册/执行/统计
    异常时 Permit 释放
"""

import asyncio

import pytest

from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.execution import (
    ToolEffectClass,
    ToolExecutionSettings,
    ToolExecutionSpec,
    ToolShutdownIncompleteError,
)
from app.shared.exceptions import ToolCancelledError
from tests.tool_lifecycle import StandaloneToolService as ToolService
from tests.tool_lifecycle import execution_kwargs


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

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return ToolResult(success=True, content="done")


@pytest.mark.asyncio
async def test_tool_service_limits_concurrency():
    """工具级并发不超过构造时注入的单运行/全局准入上限。"""
    reg = ToolService(max_concurrent_tools=2)
    tool = _SleepTool(delay=0.02)
    reg.register(tool)

    # 并发执行 5 次
    await asyncio.gather(*[reg.execute("sleep_tool", {}) for _ in range(5)])

    assert tool.max_active <= 2, f"并发工具数应受准入上限限制（2），实际 {tool.max_active}"
    assert tool.max_active >= 1


@pytest.mark.asyncio
async def test_tool_service_execute_basic():
    """execute 基本流程：成功返回 + 统计记录。"""
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
async def test_tool_service_releases_permit_on_error():
    """工具异常时 Permit 仍释放（finally 保证），后续调用不被占坑阻塞。"""
    reg = ToolService(max_concurrent_tools=2)

    class _FailTool(_SleepTool):
        async def execute(self, **kwargs) -> ToolResult:
            raise RuntimeError("tool boom")

    reg.register(_FailTool(delay=0))

    # 第一次异常，Permit 应释放；第二次仍能进入（不阻塞）
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

    assert boom.unloaded == 1
    assert good.unloaded == 1


async def test_shutdown_budget_does_not_discard_pending_unload():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowUnload(_ShutdownSpyTool):
        async def on_unload(self):
            self.unloaded += 1
            entered.set()
            await release.wait()

    service = ToolService(execution_settings=ToolExecutionSettings(shutdown_timeout_seconds=0.01))
    tool = SlowUnload()
    service.register(tool)
    try:
        with pytest.raises(ToolShutdownIncompleteError):
            await service.shutdown()
        assert entered.is_set()
        assert tool.unloaded == 1
    finally:
        release.set()
        await service.shutdown()
    assert tool.unloaded == 1


async def test_cancelled_shutdown_task_can_be_retried():
    class InterruptedUnload(_ShutdownSpyTool):
        async def on_unload(self):
            self.unloaded += 1
            if self.unloaded == 1:
                raise asyncio.CancelledError

    service = ToolService()
    tool = InterruptedUnload()
    service.register(tool)
    with pytest.raises(asyncio.CancelledError):
        await service.shutdown()
    await service.shutdown()
    assert tool.unloaded == 2


async def test_hard_cancel_of_shutdown_waiter_preserves_cleanup_owner():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowUnload(_ShutdownSpyTool):
        async def on_unload(self):
            self.unloaded += 1
            entered.set()
            await release.wait()

    service = ToolService()
    tool = SlowUnload()
    service.register(tool)
    waiter = asyncio.create_task(service.shutdown())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not service._shutdown_task.done()
    release.set()
    await service.shutdown()
    assert tool.unloaded == 1


async def test_cancel_during_refresh_returns_control_without_starting_tool(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    service = ToolService()
    tool = _SleepTool(delay=0)
    service.register(tool)
    kwargs = execution_kwargs()

    async def slow_scan():
        entered.set()
        await release.wait()

    monkeypatch.setattr(service._external_loader, "_scan_once", slow_scan)
    task = asyncio.create_task(service.execute(tool.name, {}, **kwargs))
    await entered.wait()
    kwargs["call"].cancel_events[0].set()
    try:
        with pytest.raises(ToolCancelledError):
            await asyncio.wait_for(task, 1)
        assert tool.max_active == 0
        assert not service._external_loader._refresh_task.done()
    finally:
        release.set()
        await service.shutdown()
