"""执行钩子管理。"""

import asyncio
import copy
import inspect
from collections.abc import Callable
from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.platform.observability.logger import get_logger

logger = get_logger("tools.hooks")


class ExecutionHooks:
    """执行钩子：注册 + 运行（同步/异步，单钩子失败不阻断）。"""

    def __init__(self) -> None:
        self._hooks: list[Callable] = []

    def add(self, hook: Callable) -> None:
        """添加钩子。签名：async def hook(tool_name, parameters, result)。

        钩子应为 async 函数（同步钩子在事件循环内执行，阻塞 IO 会阻塞主循环）。
        """
        self._hooks.append(hook)

    async def run(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        result: ToolResult,
        *,
        timeout: float | None = None,
    ) -> None:
        """在共享观察预算内运行钩子；异常或超时不影响主流程。

        每个钩子取得独立快照，不能改写后续钩子或调用者持有的结果。同步钩子
        必须是非阻塞计算；需要等待 I/O 的钩子必须声明为 async。
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        for hook in self._hooks:
            try:
                hook_parameters = copy.deepcopy(parameters)
                hook_result = copy.deepcopy(result)
                if inspect.iscoroutinefunction(hook):
                    if deadline is None:
                        await hook(tool_name, hook_parameters, hook_result)
                    else:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            logger.warning("工具钩子观察预算已耗尽")
                            break
                        await asyncio.wait_for(
                            hook(tool_name, hook_parameters, hook_result),
                            timeout=remaining,
                        )
                else:
                    hook(tool_name, hook_parameters, hook_result)
            except TimeoutError:
                logger.warning("工具钩子观察超时")
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("钩子执行失败: %s", e)
