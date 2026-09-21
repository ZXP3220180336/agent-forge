"""reasoning 共享执行护栏的类型与优先级契约。"""

import asyncio
import time
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from app.domain.reasoning._common import GuardResult, evaluate_guard
from app.shared.error_handling import AgentErrorKind
from app.shared.exceptions import ContextWindowExceededError


def _context_error() -> ContextWindowExceededError:
    return ContextWindowExceededError(
        model_key="main",
        input_tokens=101,
        input_budget=100,
        max_tokens=10,
    )


def test_evaluate_guard_returns_none_when_all_guards_allow() -> None:
    assert (
        evaluate_guard(
            cancel_event=None,
            deadline=None,
            cost_limiter=None,
            running_usage={},
        )
        is None
    )


def test_guard_result_is_immutable_and_uses_agent_error_kind() -> None:
    result = GuardResult(AgentErrorKind.CANCELLED, "用户取消")

    assert result.kind is AgentErrorKind.CANCELLED
    with pytest.raises(FrozenInstanceError):
        result.message = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("cancelled", "expired", "over_cost", "expected"),
    [
        (True, True, True, AgentErrorKind.CANCELLED),
        (False, True, True, AgentErrorKind.TIMEOUT),
        (False, False, True, AgentErrorKind.COST_EXCEEDED),
        (False, False, False, AgentErrorKind.CONTEXT_EXCEEDED),
    ],
)
def test_evaluate_guard_priority(
    cancelled: bool,
    expired: bool,
    over_cost: bool,
    expected: AgentErrorKind,
) -> None:
    cancel_event = asyncio.Event()
    if cancelled:
        cancel_event.set()
    cost_limiter = Mock()
    cost_limiter.check.return_value = (over_cost, 1.25)

    result = evaluate_guard(
        cancel_event=cancel_event,
        deadline=time.monotonic() - 1 if expired else None,
        cost_limiter=cost_limiter,
        running_usage={"total_tokens": 10},
        context_error=_context_error(),
    )

    assert result is not None
    assert result.kind is expected
    if expected in {AgentErrorKind.CANCELLED, AgentErrorKind.TIMEOUT}:
        cost_limiter.check.assert_not_called()
    elif expected is AgentErrorKind.COST_EXCEEDED:
        assert result.cost_usd == 1.25
        cost_limiter.check.assert_called_once_with({"total_tokens": 10})
    else:
        cost_limiter.check.assert_called_once_with({"total_tokens": 10})


def test_explicit_abort_flags_follow_the_same_priority() -> None:
    result = evaluate_guard(
        cancel_event=None,
        deadline=None,
        cost_limiter=None,
        running_usage={},
        cancelled=True,
        deadline_exceeded=True,
    )

    assert result is not None
    assert result.kind is AgentErrorKind.CANCELLED


def test_guard_returns_none_on_the_healthy_path() -> None:
    """limiter 已注入且未超限、无其他信号 → None（生产最常走的热路径）。"""
    cost_limiter = Mock()
    cost_limiter.check.return_value = (False, 0.0)

    result = evaluate_guard(
        cancel_event=None,
        deadline=time.monotonic() + 60,
        cost_limiter=cost_limiter,
        running_usage={"total_tokens": 10},
    )

    assert result is None
    cost_limiter.check.assert_called_once_with({"total_tokens": 10})


def test_explicit_deadline_flag_alone_yields_timeout() -> None:
    """仅 deadline_exceeded=True（无 cancel、deadline 未到）→ TIMEOUT。"""
    result = evaluate_guard(
        cancel_event=asyncio.Event(),  # 未置位
        deadline=time.monotonic() + 60,
        cost_limiter=None,
        running_usage={},
        deadline_exceeded=True,
    )

    assert result is not None
    assert result.kind is AgentErrorKind.TIMEOUT


def test_context_result_uses_the_same_typed_contract() -> None:
    error = _context_error()
    result = evaluate_guard(
        cancel_event=None,
        deadline=None,
        cost_limiter=None,
        running_usage={},
        context_error=error,
    )

    assert result is not None
    assert result.kind is AgentErrorKind.CONTEXT_EXCEEDED
    assert result.message.startswith("请求上下文超限")


def test_deadline_is_exceeded_at_exact_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.domain.reasoning._common.time.monotonic", lambda: 42.0)

    result = evaluate_guard(
        cancel_event=None,
        deadline=42.0,
        cost_limiter=None,
        running_usage={},
    )

    assert result is not None
    assert result.kind is AgentErrorKind.TIMEOUT
