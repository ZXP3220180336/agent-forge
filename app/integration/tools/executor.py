"""工具执行器：信号量 + 重试 + 超时 + 校验 + 统计 + 钩子。"""

import asyncio
import json
import time
from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.hooks import ExecutionHooks
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.stats import ToolStatsCollector


class ToolExecutor:
    """执行编排：在信号量内运行工具（重试/超时/校验），记录统计并触发钩子。

    依赖注入 registry（找工具）/ stats（统计记录）/ hooks（成功通知），
    是原 ToolService 执行逻辑的独立组件。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        stats: ToolStatsCollector,
        hooks: ExecutionHooks,
        *,
        max_concurrent_tools: int = 3,
        tool_timeout: int = 30,
        tool_max_retries: int = 3,
    ) -> None:
        self._registry = registry
        self._stats = stats
        self._hooks = hooks
        self._tool_timeout = tool_timeout
        self._tool_max_retries = tool_max_retries
        # 信号量必须构造期创建（asyncio.Semaphore 与事件循环绑定）
        self._tool_semaphore = asyncio.Semaphore(max_concurrent_tools)

    async def execute(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
    ) -> ToolResult:
        """执行工具（信号量最外层，包裹含重试退避的完整流程）。

        async with 天然保证异常/取消时释放信号量，不会挂死占坑。
        """
        async with self._tool_semaphore:
            return await self._execute_impl(
                name,
                parameters,
                timeout=timeout,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )

    async def _execute_impl(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
    ) -> ToolResult:
        """执行工具（带参数验证、自动重试、执行统计），在信号量保护内调用。"""

        # 1. 使用配置默认值（调用方传 None 则走注入配置）
        timeout = timeout if timeout is not None else self._tool_timeout
        max_retries = (
            max_retries if max_retries is not None else self._tool_max_retries
        )

        # 2. 查找工具
        tool = self._registry.get(name)
        if not tool:
            return ToolResult(
                success=False,
                content="",
                error=f"工具 '{name}' 未注册",
            )

        # 2. 解析参数
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except json.JSONDecodeError as e:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"参数 JSON 解析失败: {e}",
                )

        # 3. 参数前置验证
        if not tool.validate_parameters(**parameters):
            return ToolResult(
                success=False,
                content="",
                error=f"参数验证失败: {parameters!s}",
            )

        # 4. 执行工具（带超时保护、自动重试）
        last_error: str | None = None
        last_result: ToolResult | None = None
        actual_retries = 0

        for attempt in range(max_retries):
            start_time = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    tool.execute(**parameters),
                    timeout=timeout,
                )

                # 填充执行元数据
                elapsed = time.monotonic() - start_time
                result.execution_time = round(elapsed, 4)
                result.retry_count = attempt

                if result.success:
                    # 执行成功 → 记录统计、执行钩子、返回
                    self._stats.record(name, success=True, elapsed=elapsed)
                    await self._hooks.run(name, parameters, result)
                    return result

                # 执行返回失败（如文件不存在）→ 记录错误，准备重试
                last_error = result.error or "工具执行失败"
                last_result = result
                self._stats.record(name, success=False, elapsed=elapsed)

            except TimeoutError:
                last_error = f"工具执行超时（{timeout}秒）"
                elapsed = time.monotonic() - start_time
                self._stats.record(name, success=False, elapsed=elapsed)

            except Exception as e:
                last_error = f"工具执行异常: {e!s}"
                elapsed = time.monotonic() - start_time
                self._stats.record(name, success=False, elapsed=elapsed)

            actual_retries += 1

            # 重试前等待（渐进式退避：1s, 2s, 4s...）
            if attempt < max_retries - 1:
                wait = retry_delay * (2**attempt)
                await asyncio.sleep(wait)

        # 所有重试均失败
        result = last_result or ToolResult(
            success=False,
            content="",
            error=last_error,
            retry_count=actual_retries,
        )
        result.retry_count = actual_retries
        result.execution_time = result.execution_time or 0.0
        return result
