"""CostLimiter 单元测试（成本上限判定，经 LLMGateway.calculate_cost 取成本估算）。"""

import pytest

from app.application.context.cost_limiter import CostLimiter
from app.integration.llm.cost_tracker import CostTracker


class _FakeLLM:
    """结构实现 LLMGateway.calculate_cost（镜像 LLMService 静态代理 CostTracker）。"""

    @staticmethod
    def calculate_cost(usage, model=""):
        return CostTracker.calculate(usage, model)


def _limiter(ceiling: float | None, model: str = "gpt-4") -> CostLimiter:
    return CostLimiter(ceiling=ceiling, llm=_FakeLLM(), model=model)


def test_check_below_ceiling_returns_false():
    """成本 < 上限 → (False, cost)。"""
    limiter = _limiter(ceiling=1.0)
    usage = {"prompt_tokens": 100, "completion_tokens": 100}
    # gpt-4：100*0.03/1000 + 100*0.06/1000 = 0.003 + 0.006 = 0.009
    exceeded, cost = limiter.check(usage)
    assert exceeded is False
    assert cost == pytest.approx(0.009)


def test_check_at_ceiling_returns_false():
    """成本恰等于上限 → 不超限（严格 > 语义）。"""
    # gpt-4：1000 prompt + 500 completion = 0.03 + 0.03 = 0.06
    limiter = _limiter(ceiling=0.06)
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    exceeded, cost = limiter.check(usage)
    assert exceeded is False
    assert cost == pytest.approx(0.06)


def test_check_over_ceiling_returns_true():
    """成本 > 上限 → (True, cost)。"""
    limiter = _limiter(ceiling=0.05)
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    exceeded, cost = limiter.check(usage)
    assert exceeded is True
    assert cost == pytest.approx(0.06)


def test_ceiling_none_disables():
    """ceiling=None → 恒 (False, cost)，不启用。"""
    limiter = _limiter(ceiling=None)
    usage = {"prompt_tokens": 10_000, "completion_tokens": 10_000}
    exceeded, cost = limiter.check(usage)
    assert exceeded is False
    assert cost > 0  # 仍返回折算成本，供观测


def test_check_uses_model_pricing():
    """model 定价与默认均价兜底结果不同（model 经 CostLimiter 传给 calculate_cost）。"""
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    # gpt-4o：0.0025 + 0.01 = 0.0125
    _, model_cost = _limiter(ceiling=1.0, model="gpt-4o").check(usage)
    # 默认均价：0.002 + 0.008 = 0.01
    _, default_cost = _limiter(ceiling=1.0, model="").check(usage)
    assert model_cost == pytest.approx(0.0125)
    assert default_cost == pytest.approx(0.01)
    assert model_cost != default_cost


def test_zero_usage_cost_zero():
    """空/零用量 → 成本 0、不超限。"""
    limiter = _limiter(ceiling=0.0)
    exceeded, cost = limiter.check({})
    assert cost == 0.0
    assert exceeded is False


def test_accumulated_usage_is_cumulative_cost():
    """累计 usage（跨轮累计字典）折算即累计成本——与单轮无关。"""
    limiter = _limiter(ceiling=1.0)
    round1 = {"prompt_tokens": 500, "completion_tokens": 250}
    round2 = {"prompt_tokens": 500, "completion_tokens": 250}
    # 单轮 cost = 0.015 + 0.015 = 0.03；累计两轮 = 0.06
    _, cost1 = limiter.check(round1)
    cumulative = {
        "prompt_tokens": round1["prompt_tokens"] + round2["prompt_tokens"],
        "completion_tokens": round1["completion_tokens"] + round2["completion_tokens"],
    }
    _, cost2 = limiter.check(cumulative)
    assert cost1 == pytest.approx(0.03)
    assert cost2 == pytest.approx(0.06)
