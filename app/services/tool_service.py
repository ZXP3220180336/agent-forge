"""
工具服务层
管理全局工具注册中心，负责内置工具的注册与查询。
"""

import importlib

from app.tools import ToolRegistry, tool_registry
from app.tools.builtin import __all__ as builtin_tool_names


class ToolService:
    """
    工具服务。

    职责：
    - 持有全局工具注册中心单例
    - 启动时注册全部内置工具（幂等）
    - 提供注册中心访问入口（供 Agent / Tool API 路由使用）
    """

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self._registry = registry or tool_registry

    @property
    def registry(self) -> ToolRegistry:
        """全局工具注册中心"""
        return self._registry

    def init_default_tools(self) -> list[str]:
        """
        注册全部内置工具（幂等），返回本次新增的工具名。

        工具实例化不依赖外部服务（API Key 在执行时才需要），
        因此注册失败只会导致个别工具不可用，不影响启动。
        """
        pkg = importlib.import_module("app.tools.builtin")
        registered: list[str] = []
        for name in builtin_tool_names:
            if self._registry.get(name) is None:  # 幂等：跳过已注册
                tool_cls = getattr(pkg, name)
                self._registry.register(tool_cls())
                registered.append(name)
        return registered

    def list_tools(self) -> list[str]:
        """已注册工具名列表"""
        return self._registry.list_tools()

    def get_stats(self):
        """工具执行统计（供 Tool API 路由使用）"""
        return self._registry.get_all_stats_summary()
