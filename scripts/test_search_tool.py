import asyncio

from app.services import ToolService
from app.tools.base import BaseTool
from app.tools.builtin import SearchTool
from app.tools.builtin import __all__ as builtin_tools


async def demo():
    reg = ToolService()

    # 1. 注册工具
    reg.register(SearchTool())

    # 2. 执行工具
    result = await reg.execute("search", {"query": "Python asyncio 教程"})
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

    pkg = importlib.import_module("app.tools.builtin")
    for tool_name in builtin_tools:
        tool_cls: type[BaseTool] = getattr(pkg, tool_name)
        reg.register(tool_cls())

    # 查看已注册的工具
    print("已注册工具:", reg.list_tools())


asyncio.run(demo())
