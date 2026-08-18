"""工具服务 Facade — 聚合 Registry/Selector/Executor/Validator/ResultProcessor/Auditor/Stats/Hooks/Assembler。

对外保持稳定 API（满足 ToolGateway：get_openai_tools + execute），
具体职责委托给内部组件，与既有调用方（Agent / 路由 / 测试）兼容。
"""

from collections.abc import Callable
from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.assembler import ToolAssembler
from app.integration.tools.base import BaseTool
from app.integration.tools.executor import ToolExecutor
from app.integration.tools.hooks import ExecutionHooks
from app.integration.tools.loader import ExternalToolLoader
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.result_processor import ResultProcessor
from app.integration.tools.security import ApprovalGate, RiskLevel, ToolAuditor
from app.integration.tools.selector import DefaultToolSelector, ToolSelector
from app.integration.tools.stats import ToolStats, ToolStatsCollector
from app.integration.tools.validator import ParameterValidator


class ToolService:
    """工具服务统一入口：组合各职责组件，对外暴露统一 API。"""

    def __init__(
        self,
        max_concurrent_tools: int = 3,
        tool_timeout: int = 30,
        tool_max_retries: int = 3,
        *,
        selector: ToolSelector | None = None,
        validator: ParameterValidator | None = None,
        result_processor: ResultProcessor | None = None,
        auditor: ToolAuditor | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> None:
        self._registry = ToolRegistry()
        self._stats = ToolStatsCollector()
        self._hooks = ExecutionHooks()
        self._assembler = ToolAssembler()
        self._selector = selector or DefaultToolSelector()
        self._executor = ToolExecutor(
            self._registry,
            self._stats,
            self._hooks,
            validator=validator,
            result_processor=result_processor,
            auditor=auditor,
            approval_gate=approval_gate,
            max_concurrent_tools=max_concurrent_tools,
            tool_timeout=tool_timeout,
            tool_max_retries=tool_max_retries,
        )
        # 外部工具热加载器（execute 惰性检查：无后台任务，见 loader.py）
        self._external_loader = ExternalToolLoader(self)

    # ===== 注册管理（→ Registry + Stats 双写） =====

    def register(self, tool: BaseTool) -> None:
        """注册工具；重名抛 ValueError。"""
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
        selected = self._selector.select(self._registry.all_tools())
        return [tool.to_openai_tool() for tool in selected]

    def get_openai_responses(self) -> list[dict[str, Any]]:
        """OpenAI Response Schema 列表（全量，不走选择器）。"""
        return self._registry.get_openai_responses()

    # ===== 工具执行（→ Executor） =====

    async def execute(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
    ) -> ToolResult:
        """执行工具（信号量 + 参数验证 + 自动重试 + 超时 + 统计 + 截断 + 审计 + 钩子）。

        入口先做外部工具惰性检查（目录变化 → 重扫），对齐「变更 → 下次调用生效」。
        """
        await self._external_loader.maybe_refresh()
        return await self._executor.execute(
            name,
            parameters,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )

    async def refresh_external_tools(self) -> None:
        """手动触发外部工具重扫（加载新增 / 重载修改 / 卸载删除）。供未来管理接口。"""
        await self._external_loader.scan_once()

    # ===== 统计（→ Stats） =====

    def get_stats(
        self, name: str | None = None
    ) -> dict[str, ToolStats] | ToolStats | None:
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
