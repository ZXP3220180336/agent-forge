"""成本限流器（结构实现 CostLimiterPort）。

应用层实现：依赖领域端口 `LLMGateway` 获得「usage + model → 成本（USD）」折算
（`calculate_cost`，由 LLM 模块 Facade `LLMService` 结构实现）——应用层不直接
import 集成层，成本估算归属 LLM 能力、经 LLM 网关端口接入（对齐 ContextManager
经 `LLMGateway.count_*` 端口先例）。按 ceiling 判定超限。无状态纯函数——容器可安全
共享单例。
"""

from app.domain.ports.llm_gateway import LLMGateway


class CostLimiter:
    """成本上限判定器（结构实现 CostLimiterPort，无状态纯函数）。

    Args:
        ceiling: 成本上限（美元）；None=不启用（恒判定不超限）。
        llm: LLM 网关端口（成本估算 `calculate_cost` 来源）。
        model: 定价查找键（传给 calculate_cost）；空串走默认均价兜底。
    """

    def __init__(
        self,
        ceiling: float | None,
        llm: LLMGateway,
        model: str = "",
    ) -> None:
        self.ceiling = ceiling
        self.model = model
        self._llm = llm

    def check(self, usage: dict) -> tuple[bool, float]:
        """折算累计 usage 成本并与 ceiling 比较（严格大于超限）。"""
        cost = self._llm.calculate_cost(usage, self.model)["cost_usd"]
        exceeded = self.ceiling is not None and cost > self.ceiling
        return exceeded, cost
