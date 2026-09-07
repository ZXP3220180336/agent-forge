"""请求级上下文预算闸测试。"""

import pytest

from app.integration.llm.request_budget import (
    RequestBudgetConfig,
    RequestBudgetGuard,
    RequestBudgetManager,
)
from app.shared.exceptions import ContextWindowExceededError, ParameterValidationError


@pytest.fixture(autouse=True)
def _isolate_manager_state():
    """用例间隔离 RequestBudgetManager 类级缓存（对齐 test_container 快照模式）。"""
    saved = {
        "_configs": dict(RequestBudgetManager._configs),
        "_instances": dict(RequestBudgetManager._instances),
        "_default_config": RequestBudgetManager._default_config,
    }
    yield
    RequestBudgetManager._configs = saved["_configs"]
    RequestBudgetManager._instances = saved["_instances"]
    RequestBudgetManager._default_config = saved["_default_config"]


def test_request_budget_includes_tools_response_format_and_output_reserve():
    """最终请求的 tools/schema 与输出预留均应占用模型窗口。"""
    guard = RequestBudgetGuard(
        "main",
        RequestBudgetConfig(context_window_tokens=80, safety_margin_tokens=4)
    )
    request = {
        "messages": [{"role": "user", "content": "短问题"}],
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {"payload": {"type": "string", "description": "x" * 300}}}}}],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
    }

    with pytest.raises(ContextWindowExceededError) as exc_info:
        guard.validate("deepseek-v4-pro", request)

    assert exc_info.value.input_tokens > exc_info.value.input_budget
    assert exc_info.value.max_tokens == 16


def test_request_budget_allows_request_within_effective_input_budget():
    """预算内请求应返回可观测的估算结果。"""
    guard = RequestBudgetGuard(
        "fast",
        RequestBudgetConfig(context_window_tokens=1_000, safety_margin_tokens=20)
    )

    result = guard.validate(
        "deepseek-v4-pro",
        {"messages": [{"role": "user", "content": "分析批次 A"}], "max_tokens": 100},
    )

    assert result.input_tokens > 0
    assert result.input_tokens <= result.input_budget


def test_request_budget_manager_caches_guard_by_model_key():
    """窗口配置按 model_key 解析并缓存，LLMService 不传递领域侧预算参数。"""
    previous = dict(RequestBudgetManager._configs)
    try:
        RequestBudgetManager.register_config(
            {"fast": RequestBudgetConfig(512, 16)}
        )
        fast = RequestBudgetManager.get("fast")
        assert fast is RequestBudgetManager.get("fast")
        assert fast is not RequestBudgetManager.get("main")
    finally:
        RequestBudgetManager.register_config(previous)


@pytest.mark.parametrize("window,margin", [(0, 0), (-1, 0), (10, -1), (10, 10)])
def test_invalid_config_fails_at_registration_boundary(window, margin):
    with pytest.raises(ParameterValidationError):
        RequestBudgetConfig(window, margin)


def test_manager_unknown_key_uses_default_config():
    """未注册 model_key 使用默认窗口（128000/1024）兜底，小请求放行。"""
    RequestBudgetManager.reset()
    guard = RequestBudgetManager.get("unknown-key")
    assert guard is RequestBudgetManager.get("unknown-key"), "缺失键实例也应缓存"
    assert guard._config == RequestBudgetManager._default_config

    result = guard.validate("gpt-4", {"messages": [], "max_tokens": 16})
    assert result.input_tokens <= result.input_budget


def test_reset_rebuilds_instances_without_discarding_configuration():
    previous = dict(RequestBudgetManager._configs)
    try:
        RequestBudgetManager.register_config({"fast": RequestBudgetConfig(80, 4)})
        before = RequestBudgetManager.get("fast")
        RequestBudgetManager.reset()
        after = RequestBudgetManager.get("fast")
        assert after is not before
        with pytest.raises(ContextWindowExceededError):
            after.validate("gpt-4", {"messages": [], "max_tokens": 100})
    finally:
        RequestBudgetManager.register_config(previous)


def test_sampling_parameters_do_not_consume_context():
    guard = RequestBudgetGuard("main", RequestBudgetConfig(1000, 4))
    request = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    original = guard.validate("gpt-4", request)
    request.update(temperature=0.8, seed=123, metadata={"trace": "x" * 10000})
    assert guard.validate("gpt-4", request) == original


def test_special_token_spelling_is_plain_user_text():
    guard = RequestBudgetGuard("main", RequestBudgetConfig(1000, 4))
    assert guard.validate(
        "gpt-4",
        {"messages": [{"role": "user", "content": "<|endoftext|>"}], "max_tokens": 10},
    ).input_tokens > 0


@pytest.mark.parametrize("output", [None, -1, 0, True, "20", 1.5])
def test_output_reservation_requires_positive_integer(output):
    guard = RequestBudgetGuard("main", RequestBudgetConfig(1000, 4))
    with pytest.raises(ParameterValidationError):
        guard.validate("gpt-4", {"messages": [], "max_tokens": output})
