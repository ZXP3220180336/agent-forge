"""
CostTracker 单元测试

覆盖 _find_price 定价查找的确定性行为（精确匹配 → 最长前缀匹配 → 默认价）：
    - 精确匹配优先于前缀匹配
    - 前缀匹配按「最长前缀优先」（sorted key=len reverse），非插入顺序
    - 未知模型回退默认价
    - 空模型回退默认价

目的：MODEL_PRICING 定价表是模块内固定表，未来改价/新增 key 时若破坏
这些行为（如引入等长前缀让前缀匹配歧义、或误改匹配顺序），测试应能拦截。
"""

import pytest

from app.services.llm.cost_tracker import MODEL_PRICING, CostTracker


# =====================================================================
# 精确匹配
# =====================================================================


def test_exact_match_takes_precedence_over_prefix():
    """精确匹配优先于前缀匹配：deepseek-chat 不因是 deepseek-chat-xxx 的前缀而命中错误价。"""
    price = CostTracker._find_price("deepseek-chat")
    assert price == MODEL_PRICING["deepseek-chat"], (
        f"精确匹配应命中 deepseek-chat 定价，实际 {price}"
    )


def test_exact_match_all_pricing_keys():
    """MODEL_PRICING 所有 key 都能被精确命中（无遗漏/无歧义）。"""
    for model in MODEL_PRICING:
        assert CostTracker._find_price(model) == MODEL_PRICING[model], (
            f"定价表 key {model} 应精确命中"
        )


# =====================================================================
# 最长前缀匹配（确定性，非插入顺序）
# =====================================================================


def test_longest_prefix_wins():
    """最长前缀优先：deepseek-chat-v2 → deepseek-chat 定价（不被更短/等长前缀劫持）。"""
    price = CostTracker._find_price("deepseek-chat-v2")
    assert price == MODEL_PRICING["deepseek-chat"], (
        f"deepseek-chat-v2 应按最长前缀命中 deepseek-chat，实际 {price}"
    )


def test_prefix_match_independent_of_dict_insertion_order(monkeypatch):
    """前缀匹配结果不依赖定价表插入顺序：sorted(key=len, reverse=True) 重新排序。

    回归防护：未来改 pricing 表（新增/重排 key）时，若依赖插入顺序做前缀匹配
    会得到不确定结果。本测试注入一个**乱序**的 pricing 表（deepseek-chat 排最后），
    验证 deepseek-chat-v2 仍按最长前缀命中 deepseek-chat。
    """
    # 乱序注入：把 deepseek-chat 放到字典末尾，模拟「新增 key 打乱插入序」
    test_pricing = {
        "gpt-4o": MODEL_PRICING["gpt-4o"],
        "deepseek-reasoner": MODEL_PRICING["deepseek-reasoner"],
        "deepseek-chat": {"input": 1.0, "output": 2.0},  # 覆盖原值便于断言，且排在末尾
    }
    monkeypatch.setattr(
        "app.services.llm.cost_tracker.MODEL_PRICING",
        test_pricing,
    )
    # deepseek-chat-v2 同时以 deepseek-chat / deepseek-reasoner 为前缀（不等长），
    # sorted(key=len, reverse=True) 保证最长前缀 deepseek-chat 优先，与插入序无关。
    assert CostTracker._find_price("deepseek-chat-v2") == test_pricing["deepseek-chat"], (
        "最长前缀匹配应优先于短前缀，不依赖字典插入顺序"
    )


# =====================================================================
# 回退默认价
# =====================================================================


def test_unknown_model_returns_default_price():
    """未知模型（无精确/前缀匹配）→ 回退默认均价。"""
    assert CostTracker._find_price("nonexistent-model-xyz") == CostTracker.DEFAULT_PRICE


def test_empty_model_returns_default_price():
    """空模型名 → 回退默认均价（不抛异常）。"""
    assert CostTracker._find_price("") == CostTracker.DEFAULT_PRICE
