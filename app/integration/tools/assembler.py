"""内置工具装配。"""

import importlib

from app.integration.tools.builtin import __all__ as builtin_tool_names
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.stats import ToolStatsCollector
from app.platform.observability.logger import get_logger

logger = get_logger("tools.assembler")


class ToolAssembler:
    """装配：importlib 扫描 builtin 包，实例化并注册（同步初始化统计）。"""

    def assemble(
        self,
        registry: ToolRegistry,
        stats: ToolStatsCollector,
    ) -> list[str]:
        """注册全部内置工具（幂等），返回本次新增的类名列表。

        工具实例化不依赖外部服务（API Key 在执行时才需要），
        因此注册失败只会导致个别工具不可用，不影响启动。
        """
        pkg = importlib.import_module("app.integration.tools.builtin")
        registered: list[str] = []
        for name in builtin_tool_names:
            tool_cls = getattr(pkg, name)
            try:
                tool = tool_cls()
                # 幂等：按注册 key（实例 tool.name）判断，而非类名——类名与 tool.name 不同
                if registry.get(tool.name) is not None:
                    continue
                registry.register(tool)
                stats.init(tool.name)
                registered.append(name)
            except Exception as e:  # noqa: BLE001 — 单工具注册失败不影响启动（docstring 契约）
                logger.warning("内置工具注册失败，跳过: %s: %s", name, e)
        return registered
