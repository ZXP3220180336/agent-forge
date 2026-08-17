"""执行钩子管理。"""

import inspect
from collections.abc import Callable
from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.platform.observability.logger import get_logger

logger = get_logger("services.tool_service")


class ExecutionHooks:
    """执行钩子：注册 + 运行（同步/异步，单钩子失败不阻断）。"""

    def __init__(self) -> None:
        self._hooks: list[Callable] = []

    def add(self, hook: Callable) -> None:
        """添加钩子。签名：async def hook(tool_name, parameters, result)。"""
        self._hooks.append(hook)

    async def run(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """运行全部钩子；单个钩子异常只记 warning，不影响主流程。"""
        for hook in self._hooks:
            try:
                if inspect.iscoroutinefunction(hook):
                    await hook(tool_name, parameters, result)
                else:
                    hook(tool_name, parameters, result)
            except Exception as e:  # noqa: BLE001
                logger.warning("钩子执行失败: %s", e)
