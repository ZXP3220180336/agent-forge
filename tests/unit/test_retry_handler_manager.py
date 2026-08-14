"""
RetryHandlerManager 单元测试

覆盖「按 model_key 跨请求共享 RetryHandler（含 CircuitBreaker）」决策：
    同 key 返回同一实例（熔断窗口跨请求积累）
    不同 key 返回不同实例（main/reasoning/fast 独立熔断）
    未知 key 抛 ValueError
    reset 后重新建实例
"""

import pytest

from app.integration.llm.retry import RetryHandlerManager


# =====================================================================
# RetryHandlerManager
# =====================================================================


def test_manager_same_key_shared_instance():
    """同一 model_key 返回同一实例（共享熔断窗口跨请求积累）。"""
    RetryHandlerManager.reset()
    a = RetryHandlerManager.get("main")
    b = RetryHandlerManager.get("main")
    assert a is b, "同 key 必须复用同一 RetryHandler（熔断窗口不能每次 new）"
    # 熔断器也共享（同一实例）
    assert a.circuit_breaker is b.circuit_breaker


def test_manager_different_key_isolated():
    """不同 model_key 返回不同实例（main/reasoning/fast 独立熔断）。"""
    RetryHandlerManager.reset()
    main = RetryHandlerManager.get("main")
    reasoning = RetryHandlerManager.get("reasoning")
    fast = RetryHandlerManager.get("fast")
    assert main is not reasoning
    assert main is not fast
    assert reasoning is not fast


def test_manager_custom_key_lazy_builds():
    """任意 model_key（含未预定义）都懒构建，不再抛 ValueError（对齐 ClientManager）。"""
    RetryHandlerManager.reset()
    handler = RetryHandlerManager.get("custom_key")
    assert handler is not None, "未知 key 应懒构建返回 handler"
    assert RetryHandlerManager.get("custom_key") is handler, "同 key 复用实例"


def test_manager_reset_clears_cache():
    """reset 后重新建实例。"""
    RetryHandlerManager.reset()
    a = RetryHandlerManager.get("main")
    RetryHandlerManager.reset()
    b = RetryHandlerManager.get("main")
    assert a is not b, "reset 后应新建实例"


def test_manager_failure_accumulates_across_requests():
    """熔断窗口跨请求积累：同 key 多次失败，failure_count 持续增长。

    验证「按 key 共享」修复了熔断失效的隐性缺陷——若每次请求新建熔断器，
    第二次请求的 failure_count 会回到 0。
    """
    RetryHandlerManager.reset()
    cb = RetryHandlerManager.get("main").circuit_breaker
    # 第一次请求失败
    cb.record_failure()
    assert cb.failure_count == 1
    # 第二次请求（模拟下一个调用复用同一实例）失败 → 窗口累计
    cb.record_failure()
    assert cb.failure_count == 2
    # 第三次请求（仍是同一实例）→ 累计 3
    cb.record_failure()
    assert cb.failure_count == 3


def test_manager_failure_isolated_by_key():
    """不同 key 的熔断窗口互不影响：reasoning 失败不累计到 main。"""
    RetryHandlerManager.reset()
    main_cb = RetryHandlerManager.get("main").circuit_breaker
    reasoning_cb = RetryHandlerManager.get("reasoning").circuit_breaker
    reasoning_cb.record_failure()
    assert reasoning_cb.failure_count == 1
    assert main_cb.failure_count == 0, "main 的熔断窗口不应被 reasoning 失败污染"
