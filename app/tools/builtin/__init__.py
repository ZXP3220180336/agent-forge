"""
内置工具模块
自动发现所有继承 BaseTool 的工具类
"""

import importlib
import inspect
import pkgutil

from ..base import BaseTool

# 自动收集的所有工具类
_tool_classes: dict[str, type[BaseTool]] = {}


def _discover_tools() -> dict[str, type[BaseTool]]:
    """扫描 builtin 包下所有模块，自动发现 BaseTool 子类"""
    if _tool_classes:
        return _tool_classes

    package_path = __path__[0]  # type: ignore[arg-type]

    for _, module_name, _ in pkgutil.iter_modules([package_path]):
        if module_name.startswith("_"):
            continue

        try:
            module = importlib.import_module(f".{module_name}", __package__)
        except Exception:
            continue

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseTool)
                and obj is not BaseTool
                and not getattr(obj, "__abstractmethods__", None)
            ):
                _tool_classes[name] = obj

    return _tool_classes


def __getattr__(name: str) -> type[BaseTool]:
    """惰性加载：在访问属性时从自动发现结果中查找"""
    tools = _discover_tools()
    if name in tools:
        return tools[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """列出所有可访问的工具类"""
    return list(_discover_tools().keys())


# 模块加载时执行发现
__all__ = list(_discover_tools().keys())
