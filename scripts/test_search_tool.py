import asyncio
import uuid

from app.domain.ports.tool_execution import ToolCallContext
from app.domain.reasoning.tool_batch import ToolBatchCollector
from app.integration.tools.tool_service import ToolService
from app.integration.tools.base import BaseTool
from app.integration.tools.builtin import SearchTool
from app.integration.tools.builtin import __all__ as builtin_tools


async def demo():
    reg = ToolService()

    # 1. 注册工具
    reg.register(SearchTool())

    # 2. 执行工具
    suffix = uuid.uuid4().hex
    result = await reg.execute(
        "search",
        {"query": "Python asyncio 教程"},
        call=ToolCallContext(
            run_id=f"script-run-{suffix}",
            batch_id=f"script-batch-{suffix}",
            tool_call_id=f"script-call-{suffix}",
            operation_id=f"script-operation-{suffix}",
            cancel_events=(asyncio.Event(),),
            run_stop=asyncio.Event(),
        ),
        facts=ToolBatchCollector(),
    )
    print(result.content)

    # 3. 查看统计
    stats = reg.get_stats("search")
    print(f"调用次数: {stats.call_count}")  # ty:ignore[unresolved-attribute]
    print(f"成功率: {stats.success_rate:.2%}")  # ty:ignore[unresolved-attribute]
    print(f"平均耗时: {stats.avg_time:.2f}s")  # ty:ignore[unresolved-attribute]


async def main():
    reg = ToolService()

    # 根据 __all__ 中的类名动态导入并注册
    import importlib

    pkg = importlib.import_module("app.integration.tools.builtin")
    for tool_name in builtin_tools:
        tool_cls: type[BaseTool] = getattr(pkg, tool_name)
        reg.register(tool_cls())

    # 查看已注册的工具
    print("已注册工具:", reg.list_tools())


asyncio.run(demo())
