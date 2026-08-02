"""
工具注册中心
管理所有工具的注册、查找和执行
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import settings
from .base import BaseTool, ToolResult


@dataclass
class ToolStats:
    """
    工具执行统计
    """

    call_count: int = 0  # 调用次数
    success_count: int = 0  # 成功次数
    failed_count: int = 0  # 失败次数
    total_time: float = 0.0  # 总耗时（秒）
    last_call_time: float | None = None  # 最后调用时间戳

    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.call_count == 0:
            return 0.0
        return self.success_count / self.call_count

    @property
    def avg_time(self) -> float:
        """平均耗时（秒）"""
        if self.call_count == 0:
            return 0.0
        return self.total_time / self.call_count


class ToolRegistry:
    """
    工具注册中心

    功能：
    - 工具注册与注销
    - 工具查找与发现
    - 工具执行与日志
    - 工具列表导出（OpenAI 格式）
    - 自动重试与超时保护
    - 执行统计（调用次数、成功率、平均耗时）
    """

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        self._execution_hooks: list[Callable] = []
        self._stats: dict[str, ToolStats] = {}
        # 工具级并发信号量：限制单任务内最大并发工具调用数。
        # Agent 维度（GPU/服务器资源），对应配置 agent_max_concurrent_tools。
        self._tool_semaphore = asyncio.Semaphore(settings.agent_max_concurrent_tools)

    # ===== 注册管理 =====

    def register(self, tool: BaseTool) -> None:
        """
        注册工具

        Args:
            tool: 工具实例

        Raises:
            ValueError: 工具已存在
        """
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已存在")

        self._tools[tool.name] = tool
        self._stats[tool.name] = ToolStats()

    def unregister(self, name: str) -> bool:
        """
        注销工具

        Args:
            name: 工具名称

        Returns:
            bool: 是否成功注销
        """
        if name in self._tools:
            del self._tools[name]
            self._stats.pop(name, None)
            return True
        return False

    def get(self, name: str) -> BaseTool | None:
        """
        获取工具实例

        Args:
            name: 工具名称

        Returns:
            工具实例或 None
        """
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """
        列出所有已注册工具名称

        Returns:
            工具名称列表
        """
        return list(self._tools.keys())

    # ===== OpenAI 格式导出 =====

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """
        获取 OpenAI Tool 格式的工具列表

        Returns:
            OpenAI Tool Schema 列表
        """
        return [tool.to_openai_tool() for tool in self._tools.values()]

    def get_openai_responses(self) -> list[dict[str, Any]]:
        """
        获取 OpenAI Response 格式的工具列表

        Returns:
            OpenAI Response Schema 列表
        """
        return [tool.to_openai_response() for tool in self._tools.values()]

    # ===== 工具执行 =====

    async def execute(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
    ) -> ToolResult:
        """
        执行工具（带参数验证、自动重试、执行统计、并发控制）。

        工具级并发信号量限制单任务内最大并发工具调用数（agent_max_concurrent_tools）。
        async with 天然保证异常/取消时释放信号量，不会挂死占坑。

        Args:
            name: 工具名称
            parameters: 工具参数（字典或 JSON 字符串）
            timeout: 单次执行超时时间（秒）
            max_retries: 最大重试次数（含首次执行）
            retry_delay: 重试间隔（秒）

        Returns:
            ToolResult: 执行结果
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

        # 1. 使用配置默认值（调用方传 None 则走 settings）
        timeout = timeout if timeout is not None else settings.tool_timeout
        max_retries = max_retries if max_retries is not None else settings.tool_max_retries

        # 2. 查找工具
        tool = self.get(name)
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
                    self._record_stats(name, success=True, elapsed=elapsed)
                    await self._run_hooks(name, parameters, result)
                    return result

                # 执行返回失败（如文件不存在）→ 记录错误，准备重试
                last_error = result.error or "工具执行失败"
                last_result = result
                self._record_stats(name, success=False, elapsed=elapsed)

            except TimeoutError:
                last_error = f"工具执行超时（{timeout}秒）"
                elapsed = time.monotonic() - start_time
                self._record_stats(name, success=False, elapsed=elapsed)

            except Exception as e:
                last_error = f"工具执行异常: {e!s}"
                elapsed = time.monotonic() - start_time
                self._record_stats(name, success=False, elapsed=elapsed)

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

    # ===== 执行统计 =====

    def _record_stats(self, name: str, success: bool, elapsed: float) -> None:
        """记录执行统计"""
        if name not in self._stats:
            self._stats[name] = ToolStats()

        stats = self._stats[name]
        stats.call_count += 1
        stats.total_time += elapsed
        stats.last_call_time = time.time()

        if success:
            stats.success_count += 1
        else:
            stats.failed_count += 1

    def get_stats(
        self, name: str | None = None
    ) -> dict[str, ToolStats] | ToolStats | None:
        """
        获取工具执行统计

        Args:
            name: 工具名称（None 则返回全部）

        Returns:
            统计对象或全量统计字典
        """
        if name is not None:
            return self._stats.get(name)
        return dict(self._stats)

    def get_all_stats_summary(self) -> dict[str, Any]:
        """
        获取所有工具的统计摘要

        Returns:
            包含总调用数、总成功率、各工具详情
        """
        total_calls = sum(s.call_count for s in self._stats.values())
        total_success = sum(s.success_count for s in self._stats.values())
        total_failed = sum(s.failed_count for s in self._stats.values())

        return {
            "total_calls": total_calls,
            "total_success": total_success,
            "total_failed": total_failed,
            "overall_success_rate": total_success / total_calls
            if total_calls > 0
            else 0.0,
            "tools": {
                name: {
                    "call_count": s.call_count,
                    "success_rate": round(s.success_rate, 4),
                    "avg_time": round(s.avg_time, 4),
                    "last_call_time": s.last_call_time,
                }
                for name, s in self._stats.items()
            },
        }

    # ===== 钩子机制 =====

    def add_execution_hook(self, hook: Callable) -> None:
        """
        添加执行钩子函数

        钩子函数签名：async def hook(tool_name, parameters, result)

        Args:
            hook: 钩子函数
        """
        self._execution_hooks.append(hook)

    async def _run_hooks(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """
        运行所有执行钩子

        Args:
            tool_name: 工具名称
            parameters: 工具参数
            result: 执行结果
        """
        for hook in self._execution_hooks:
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook(tool_name, parameters, result)
                else:
                    hook(tool_name, parameters, result)
            except Exception as e:
                # 钩子失败不影响工具执行
                print(f"钩子执行失败: {e}")


# 全局工具注册中心实例
tool_registry = ToolRegistry()
