"""工具服务 Facade — 聚合 Registry/Selector/Executor/Validator/ResultProcessor/Auditor/Stats/Hooks/Assembler。

对外保持稳定 API（满足 ToolGateway：get_openai_tools + execute），
具体职责委托给内部组件，与既有调用方（Agent / 路由 / 测试）兼容。
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from app.domain.ports.tool_execution import ToolCallContext, ToolFactSink
from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.admission import ToolAdmission
from app.integration.tools.assembler import ToolAssembler
from app.integration.tools.base import BaseTool
from app.integration.tools.execution import (
    ToolExecutionSettings,
    ToolShutdownIncompleteError,
    check_abort,
    is_execution_enabled,
    read_execution_spec,
    wait_for_abort,
)
from app.integration.tools.executor import ToolExecutor
from app.integration.tools.hooks import ExecutionHooks
from app.integration.tools.loader import ExternalToolLoader
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.result_processor import ResultProcessor
from app.integration.tools.security import ApprovalGate, RiskLevel, ToolAuditor
from app.integration.tools.selector import DefaultToolSelector, ToolSelector
from app.integration.tools.stats import ToolStats, ToolStatsCollector
from app.integration.tools.validator import ParameterValidator
from app.platform.observability.logger import get_logger
from app.shared.exceptions import ToolRunStoppedError

logger = get_logger("tools.service")


class ToolService:
    """工具服务统一入口：组合各职责组件，对外暴露统一 API。"""

    def __init__(
        self,
        max_concurrent_tools: int = 3,
        tool_timeout: int = 30,
        tool_max_retries: int = 3,
        *,
        # 准入门禁参数一律 keyword-only：插在既有位置参数之间会让旧的位置调用静默错位。
        max_concurrent_tools_per_run: int | None = None,
        max_concurrent_tools_global: int | None = None,
        max_pending_calls: int = 30,
        max_pending_calls_per_run: int = 6,
        admission_timeout_seconds: float = 30.0,
        selector: ToolSelector | None = None,
        validator: ParameterValidator | None = None,
        result_processor: ResultProcessor | None = None,
        auditor: ToolAuditor | None = None,
        approval_gate: ApprovalGate | None = None,
        external_config_source: Callable[[str], Any] | None = None,
        tool_observation_timeout: float = 0.2,
        execution_settings: ToolExecutionSettings | None = None,
    ) -> None:
        self._registry = ToolRegistry()
        self._stats = ToolStatsCollector()
        self._hooks = ExecutionHooks()
        self._assembler = ToolAssembler()
        self._selector = selector or DefaultToolSelector()
        # max_concurrent_tools 保留为直接构造 ToolService 的程序化入口；生产配置
        # 分别注入全局和单运行限额，避免多个 Agent 各自拥有独立准入容量。
        per_run_limit = max_concurrent_tools if max_concurrent_tools_per_run is None else max_concurrent_tools_per_run
        global_limit = max_concurrent_tools if max_concurrent_tools_global is None else max_concurrent_tools_global

        self._execution_settings = execution_settings or ToolExecutionSettings(
            observation_timeout_seconds=tool_observation_timeout,
        )
        self._admission_timeout = admission_timeout_seconds
        if self._execution_settings.max_recovery_records < global_limit:
            raise ValueError("执行跟踪条目上限不能小于全局执行容量")

        self._shutdown_task: asyncio.Task[None] | None = None
        self._closing = False

        admission = ToolAdmission(
            global_limit=global_limit,
            per_run_limit=per_run_limit,
            max_pending=max_pending_calls,
            max_pending_per_run=max_pending_calls_per_run,
            admission_timeout=admission_timeout_seconds,
        )

        self._executor = ToolExecutor(
            self._registry,
            self._stats,
            self._hooks,
            validator=validator,
            result_processor=result_processor,
            auditor=auditor,
            approval_gate=approval_gate,
            max_concurrent_tools=per_run_limit,
            admission=admission,
            tool_timeout=tool_timeout,
            tool_max_retries=tool_max_retries,
            observation_timeout=tool_observation_timeout,
            execution_settings=self._execution_settings,
        )

        # 无常驻扫描器；按需刷新的临时任务由 loader 接管并在关闭时排空。
        # 配置注入：装配根绑定的 settings 读取器 → 外部工具 CONFIG_KEYS 注册
        self._external_loader = ExternalToolLoader(self, config_source=external_config_source)

    # ===== 注册管理（→ Registry + Stats 双写） =====

    def register(self, tool: BaseTool) -> None:
        """注册工具；重名抛 ValueError，参数 Schema 定义非法抛 SchemaError。"""
        self._registry.register(tool)
        self._stats.init(tool.name)

    def unregister(self, name: str) -> bool:
        """注销工具及其统计，返回是否成功。"""
        ok = self._registry.unregister(name)
        if ok:
            self._stats.remove(name)
            self._executor.prune_tool_lock(name)
        return ok

    def get(self, name: str) -> BaseTool | None:
        """获取工具实例。"""
        return self._registry.get(name)

    def is_tool_active(self, tool: BaseTool) -> bool:
        """旧版本仍被调用或后台句柄使用时，loader 必须延后卸载。"""
        return self._executor.is_tool_active(tool)

    def list_tools(self) -> list[str]:
        """列出全部工具名。"""
        return self._registry.list_tools()

    def list_by_risk(self, risk_level: RiskLevel) -> list[BaseTool]:
        """按风险等级列出工具（供安全审计 / 管理界面）。"""
        return self._registry.list_by_risk(risk_level)

    def list_by_category(self, category: str) -> list[BaseTool]:
        """按功能域列出工具（供按域选择 / 管理界面）。"""
        return self._registry.list_by_category(category)

    # ===== OpenAI 格式导出（→ Selector + Registry） =====

    def get_openai_tools(self) -> list[dict[str, Any]]:
        """OpenAI Tool Schema 列表（经选择器选出本次注入 LLM 的子集）。"""
        selected = self._selector.select(self._enabled_tools())
        return [tool.to_openai_tool() for tool in selected]

    def get_openai_responses(self) -> list[dict[str, Any]]:
        """OpenAI Response Schema 列表（全部已启用工具，不走选择器）。"""
        return [tool.to_openai_response() for tool in self._enabled_tools()]

    def _enabled_tools(self) -> list[BaseTool]:
        """无实参时不能证明可执行的工具不向模型公开；调用时仍按实参复核。"""
        enabled = []
        for tool in self._registry.all_tools():
            spec = read_execution_spec(tool.describe_execution, {})
            if is_execution_enabled(spec):
                enabled.append(tool)
        return enabled

    # ===== 工具执行（→ Executor） =====

    async def execute(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
        *,
        call: ToolCallContext,
        facts: ToolFactSink,
    ) -> ToolResult:
        """执行工具（共享准入 + 参数验证 + 自动重试 + 超时 + 统计 + 截断 + 审计 + 钩子）。

        入口先做外部工具惰性检查（目录变化 → 重扫），对齐「变更 → 下次调用生效」。
        """
        admission_deadline = time.monotonic() + self._admission_timeout

        # 刷新也属于调用生命周期；即使入口已取消，调用者仍能取得未开始事实。
        self._executor.begin_call(call, facts)
        check_abort(call)
        if self._closing:
            raise ToolRunStoppedError(run_id=call.run_id, operation_id=call.operation_id)
        await self._refresh_for_call(call, admission_deadline)
        check_abort(call)

        if self._closing:
            raise ToolRunStoppedError(run_id=call.run_id, operation_id=call.operation_id)

        tool = self._registry.get(name)
        return await self._executor.execute(
            name,
            parameters,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            call=call,
            facts=facts,
            pinned_tool=tool,
            admission_deadline=admission_deadline,
        )

    async def _refresh_for_call(self, call: ToolCallContext, admission_deadline: float) -> None:
        """
        调用可立即停止等待，插件刷新事务继续由 loader 持有。
        插件刷新可能同时服务其他调用，因此本次调用退出时，不会直接终止加载器已经接管的刷新事务。
        即：
                    |──等插件刷新完成──────────────┐
        本次调用─────|─等取消 / 业务期限 / 运行停止─┤ 谁先完成，先处理谁
                    |──────另有准入等待上限────────┘
        """
        refresh = asyncio.create_task(self._external_loader.maybe_refresh())
        # 由于 refresh 内部 shield，外部取消不会中断它；但调用者可能已经取消或超时，必须在等待前检查。
        abort = asyncio.create_task(wait_for_abort(call))
        try:
            # 等待刷新或调用取消信号或准入期限到，先到者结束等待。
            done, _ = await asyncio.wait(
                (refresh, abort),
                timeout=max(0.0, admission_deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if refresh in done:
                await refresh
            elif abort in done:
                await abort
        finally:
            # wrapper 可取消；其内部 shield 的真实加载事务由 loader.close 排空。
            refresh.cancel()
            abort.cancel()
            await asyncio.gather(refresh, abort, return_exceptions=True)

    async def refresh_external_tools(self) -> None:
        """手动触发外部工具重扫（加载新增 / 重载修改 / 卸载删除）。供未来管理接口。"""
        await self._external_loader.scan_once()

    # ===== 统计（→ Stats） =====

    def get_stats(self, name: str | None = None) -> dict[str, ToolStats] | ToolStats | None:
        """单工具统计（name 给定）或全量字典。"""
        return self._stats.get(name)

    def get_all_stats_summary(self) -> dict[str, Any]:
        """全量统计摘要：总调用数 / 成功率 / 各工具详情。"""
        return self._stats.summary()

    # ===== 钩子（→ Hooks） =====

    def add_execution_hook(self, hook: Callable) -> None:
        """添加执行钩子：async def hook(tool_name, parameters, result)。"""
        self._hooks.add(hook)

    # ===== 内置工具装配（→ Assembler） =====

    def init_default_tools(self) -> list[str]:
        """注册全部内置工具（幂等），返回本次新增的类名列表。"""
        return self._assembler.assemble(self._registry, self._stats)

    async def shutdown(self) -> None:
        """总预算内关闭；未完成则报告失败，装配根不能继续关闭被工具使用的依赖。"""
        self._closing = True

        if (
            self._shutdown_task is None
            or self._shutdown_task.cancelled()
            or (self._shutdown_task.done() and self._shutdown_task.exception() is not None)
        ):
            self._shutdown_task = asyncio.create_task(self._shutdown_resources())
            self._shutdown_task.add_done_callback(self._observe_shutdown)
        done, _ = await asyncio.wait((self._shutdown_task,), timeout=self._execution_settings.shutdown_timeout_seconds)
        if not done:
            raise ToolShutdownIncompleteError("工具关闭未完成：仍保留后台任务及其依赖")
        await self._shutdown_task

    @staticmethod
    def _observe_shutdown(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _shutdown_resources(self) -> None:
        await self._external_loader.close()
        await self._executor.close()
        for name in list(self._registry.list_tools()):
            tool = self._registry.get(name)
            if tool is None:
                continue
            try:
                await tool.on_unload()
            except Exception as e:  # noqa: BLE001
                logger.warning("工具 on_unload 失败（继续关闭）: %s: %s", name, e)
