"""
CostTracker — LLM 调用成本计算

根据模型和用量计算预估成本。
定价数据维护在此模块，需随着模型价格变动更新。

用法：
    cost = CostTracker.calculate(usage, "gpt-4")
    # → {"cost_usd": 0.015, "input_cost": 0.003, "output_cost": 0.012}
"""

from __future__ import annotations

from typing import Any

# =====================================================================
# 模型定价表（$/1K tokens）
# 来源：各模型官网，随模型发布更新
# =====================================================================

MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "o1": {"input": 0.015, "output": 0.06},
    "o3-mini": {"input": 0.0011, "output": 0.0044},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    # DeepSeek
    "deepseek-chat": {"input": 0.0005, "output": 0.001},
    "deepseek-reasoner": {"input": 0.0005, "output": 0.002},
    "deepseek-v4-flash": {"input": 0.0005, "output": 0.001},
    "deepseek-pro": {"input": 0.001, "output": 0.002},
    # Claude
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-haiku-3-5": {"input": 0.0008, "output": 0.004},
    # Embedding
    "text-embedding-3-small": {"input": 0.00002, "output": 0.0},
    "text-embedding-3-large": {"input": 0.00013, "output": 0.0},
}


class CostTracker:
    """成本计算器。"""

    DEFAULT_PRICE = {"input": 0.002, "output": 0.008}  # 默认均价

    @staticmethod
    def calculate(
        usage: dict[str, Any] | None,
        model: str = "",
    ) -> dict[str, float]:
        """
        根据用量计算成本。

        Args:
            usage: {"prompt_tokens": int, "completion_tokens": int, ...}
            model: 模型名，用于查找定价

        Returns:
            {
                "cost_usd": float,       # 总成本
                "input_cost": float,     # 输入成本
                "output_cost": float,    # 输出成本
            }
        """
        if not usage:
            return {"cost_usd": 0.0, "input_cost": 0.0, "output_cost": 0.0}

        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0

        # 查找定价，按前缀匹配
        price = CostTracker._find_price(model)

        input_cost = (prompt_tokens / 1000) * price["input"]
        output_cost = (completion_tokens / 1000) * price["output"]

        return {
            "cost_usd": round(input_cost + output_cost, 6),
            "input_cost": round(input_cost, 6),
            "output_cost": round(output_cost, 6),
        }

    @staticmethod
    def accumulate(
        stats: dict[str, float],
        cost: dict[str, float],
    ) -> dict[str, float]:
        """累计成本到会话级统计。"""
        return {
            "cost_usd": round(
                stats.get("cost_usd", 0) + cost.get("cost_usd", 0),
                6,
            ),
            "input_cost": round(
                stats.get("input_cost", 0) + cost.get("input_cost", 0),
                6,
            ),
            "output_cost": round(
                stats.get("output_cost", 0) + cost.get("output_cost", 0),
                6,
            ),
        }

    @staticmethod
    def _find_price(model: str) -> dict[str, float]:
        """通过模型名查找定价（精确匹配 → 前缀匹配 → 默认）。"""
        if not model:
            return CostTracker.DEFAULT_PRICE

        # 精确匹配
        if model in MODEL_PRICING:
            return MODEL_PRICING[model]

        # 前缀匹配（如 deepseek-chat-v2 → deepseek-chat）
        for key in sorted(MODEL_PRICING.keys(), key=len, reverse=True):
            if model.startswith(key):
                return MODEL_PRICING[key]

        return CostTracker.DEFAULT_PRICE
