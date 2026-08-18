"""工具执行器：信号量 + 重试 + 超时 + 校验 + 截断 + 审计 + 统计 + 钩子。"""

import asyncio
import json
import time
from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.hooks import ExecutionHooks
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.result_processor import ResultProcessor
from app.integration.tools.security import (
    ApprovalGate,
    AutoApprovalGate,
    RiskLevel,
    ToolAuditor,
)
from app.integration.tools.stats import ToolStatsCollector
from app.integration.tools.validator import ParameterValidator
from app.platform.observability.logger import get_logger

logger = get_logger("tools.executor")


class ToolExecutor:
    """执行编排：在信号量内运行工具（重试/超时/校验/截断/审计），记录统计并触发钩子。

    依赖注入 registry（找工具）/ stats（统计记录）/ hooks（成功通知）/
    validator（参数校验）/ result_processor（结果截断）/ auditor（审计留痕）。
    是原 ToolService 执行逻辑的独立组件。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        stats: ToolStatsCollector,
        hooks: ExecutionHooks,
        *,
        validator: ParameterValidator | None = None,
        result_processor: ResultProcessor | None = None,
        auditor: ToolAuditor | None = None,
        approval_gate: ApprovalGate | None = None,
        max_concurrent_tools: int = 3,
        tool_timeout: int = 30,
        tool_max_retries: int = 3,
    ) -> None:
        self._registry = registry
        self._stats = stats
        self._hooks = hooks
        self._validator = validator or ParameterValidator()
        self._result_processor = result_processor or ResultProcessor()
        self._auditor = auditor or ToolAuditor()
        self._approval_gate = approval_gate or AutoApprovalGate()
        self._tool_timeout = tool_timeout
        self._tool_max_retries = tool_max_retries
        # 信号量必须构造期创建（asyncio.Semaphore 与事件循环绑定）
        self._tool_semaphore = asyncio.Semaphore(max_concurrent_tools)
        # per-tool 锁：concurrency_safe=False 的工具同实例内串行化
        self._tool_locks: dict[str, asyncio.Lock] = {}

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
        """执行工具（带参数校验、自动重试、结果截断、审计留痕），在信号量保护内调用。"""
        started_at = time.monotonic()

        # 1. 查找工具
        tool = self._registry.get(name)
        if not tool:
            result = ToolResult(
                success=False,
                content="",
                error=f"工具 '{name}' 未注册",
            )
            await self._audit(
                None, parameters, result, started_at=started_at, tool_name=name
            )
            return result

        # 2. 解析执行参数：调用方显式 > 工具自声明 > 全局配置（max_retries 仅调用方 / 全局两档）
        if timeout is None:
            timeout = tool.timeout if tool.timeout is not None else self._tool_timeout
        max_retries = max_retries if max_retries is not None else self._tool_max_retries

        # 3. 解析参数（str → dict）
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except json.JSONDecodeError as e:
                result = ToolResult(
                    success=False,
                    content="",
                    error=self._result_processor.normalize_error(
                        f"参数 JSON 解析失败: {e}"
                    ),
                )
                await self._audit(tool, parameters, result, started_at=started_at)
                return result

        # 4. 参数前置校验（jsonschema 全量校验，错误可归因）
        issues = tool.validation_issues(**parameters)
        if issues:
            result = ToolResult(
                success=False,
                content="",
                error=self._result_processor.normalize_error(
                    f"参数验证失败: {'; '.join(issues)}"
                ),
            )
            await self._audit(tool, parameters, result, started_at=started_at)
            return result

        # 5. 人工审批拦截（requires_approval 工具需 ApprovalGate 确认，默认 AutoApprovalGate 放行）
        if tool.requires_approval and not await self._approval_gate.request(
            name, parameters
        ):
            result = ToolResult(
                success=False,
                content="",
                error="工具调用被拒绝：等待人工审批",
            )
            await self._audit(tool, parameters, result, started_at=started_at)
            return result

        # 6. 执行（重试循环）；concurrency_safe=False 时 per-tool 锁串行化
        if tool.concurrency_safe:
            result = await self._execute_with_retry(
                tool,
                parameters,
                timeout=timeout,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
        else:
            async with self._tool_lock(name):
                result = await self._execute_with_retry(
                    tool,
                    parameters,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                )

        # 7. 审计（每次 execute 一条最终结果）
        await self._audit(tool, parameters, result, started_at=started_at)
        return result

    async def _execute_with_retry(
        self,
        tool: BaseTool,
        parameters: dict[str, Any],
        *,
        timeout: int,
        max_retries: int,
        retry_delay: float,
    ) -> ToolResult:
        """重试循环：超时保护 + 渐进式退避 + 成功截断 + 统计 + 钩子。"""
        name = tool.name
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
                    # 统一结果截断（head+tail），随后统计、钩子、返回
                    self._result_processor.truncate_result(
                        result, max_length=tool.max_output_length
                    )
                    self._stats.record(name, success=True, elapsed=elapsed)
                    await self._hooks.run(name, parameters, result)
                    return result

                # 执行返回失败（如文件不存在）→ 记录错误，准备重试
                last_error = self._result_processor.normalize_error(
                    result.error or "工具执行失败"
                )
                last_result = result
                self._stats.record(name, success=False, elapsed=elapsed)

            except TimeoutError:
                last_error = self._result_processor.normalize_error(
                    f"工具执行超时（{timeout}秒）"
                )
                elapsed = time.monotonic() - start_time
                self._stats.record(name, success=False, elapsed=elapsed)

            except Exception as e:  # noqa: BLE001
                last_error = self._result_processor.normalize_error(
                    f"工具执行异常: {e!s}"
                )
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

    def _tool_lock(self, name: str) -> asyncio.Lock:
        """惰性 per-tool 锁（asyncio.Lock 3.10+ 不绑定事件循环，惰性创建安全）。"""
        if name not in self._tool_locks:
            self._tool_locks[name] = asyncio.Lock()
        return self._tool_locks[name]

    def prune_tool_lock(self, name: str) -> None:
        """注销工具时清理 per-tool 锁条目（由 ToolService.unregister 调用）。

        跳过仍在持有的锁：外部工具重载时在飞 execute 持锁，先 pop 会导致
        新实例惰性建新锁、与旧实例并发（破坏串行化）；跳过则新实例复用同一把锁。
        """
        lock = self._tool_locks.get(name)
        if lock is not None and not lock.locked():
            self._tool_locks.pop(name, None)

    async def _audit(
        self,
        tool: BaseTool | None,
        parameters: dict[str, Any] | str,
        result: ToolResult,
        *,
        started_at: float,
        tool_name: str | None = None,
    ) -> None:
        """统一审计出口：取工具元数据（未注册时传 None + 保留原始工具名）+ 最终 result。"""
        elapsed = time.monotonic() - started_at
        audit_params = (
            parameters
            if isinstance(parameters, dict)
            else {"raw": str(parameters)[:500]}
        )
        try:
            await self._auditor.record(
                tool_name=tool.name if tool else (tool_name or "unknown"),
                risk_level=tool.risk_level if tool else RiskLevel.L0_READONLY,
                category=tool.category if tool else "unknown",
                success=result.success,
                elapsed=elapsed,
                parameters=audit_params,
                error=result.error,
                retry_count=result.retry_count,
                content_preview=result.content,
            )
        except Exception as e:  # noqa: BLE001 — 审计失败不阻断工具执行
            logger.warning("工具审计失败（不影响执行）: %s", e)
