"""
ExecutionHooks 执行钩子单元测试

覆盖：
    同步 / 异步钩子注册与运行
    单个钩子异常不影响后续钩子与主流程
"""

import pytest

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.hooks import ExecutionHooks


async def test_sync_and_async_hooks_run_in_order():
    """同步 + 异步钩子按注册顺序运行，收到一致入参。"""
    calls: list[str] = []
    hooks = ExecutionHooks()

    def sync_hook(tool_name, parameters, result):
        calls.append(f"sync:{tool_name}:{result.success}")

    async def async_hook(tool_name, parameters, result):
        calls.append(f"async:{tool_name}:{result.success}")

    hooks.add(sync_hook)
    hooks.add(async_hook)

    result = ToolResult(success=True, content="ok")
    await hooks.run("search", {"query": "x"}, result)

    assert calls == ["sync:search:True", "async:search:True"]


async def test_hook_failure_does_not_block_others():
    """单个钩子异常仅记录，不影响后续钩子。"""
    calls: list[str] = []
    hooks = ExecutionHooks()

    def failing_hook(tool_name, parameters, result):
        raise RuntimeError("hook boom")

    def after_hook(tool_name, parameters, result):
        calls.append("after")

    hooks.add(failing_hook)
    hooks.add(after_hook)

    result = ToolResult(success=True, content="ok")
    await hooks.run("search", {}, result)

    assert calls == ["after"]  # 失败钩子不阻断后续


async def test_no_hooks_is_noop():
    """无钩子时 run 无副作用。"""
    await ExecutionHooks().run("search", {}, ToolResult(success=True, content="ok"))
