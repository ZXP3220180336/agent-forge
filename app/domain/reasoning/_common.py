"""reasoning 策略共享小工具（react/reflection/planner 共用，不 import 任何策略 → 无环）。

仅收无状态函数、常量及跨策略装饰器，不维护全局可变状态（error_handlers、策略实例态等
仍由各策略自行持有）。命名带 `_` 前缀 = 层内私有共享（非对外导出）。
"""

import asyncio
import time
from contextlib import aclosing
from dataclasses import dataclass
from functools import wraps
from typing import Any

from app.domain.ports.cost_limiter import CostLimiterPort
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.exceptions import ContextWindowExceededError


def reject_concurrent_runs(method):
    """拒绝并发复用持有可变 outcome/计数器的策略实例。"""

    @wraps(method)
    async def wrapped(self, *args: Any, **kwargs: Any):
        if getattr(self, "_execution_running", False):
            raise RuntimeError(f"同一个 {type(self).__name__} 实例不能并发执行")
        self._execution_running = True
        try:
            # 外层消费者 aclose 时也要等待内部 finally，之后才可复用实例。
            async with aclosing(method(self, *args, **kwargs)) as stream:
                async for event in stream:
                    yield event
        finally:
            self._execution_running = False

    return wrapped


@dataclass(frozen=True, slots=True)
class GuardResult:
    """领域执行护栏的类型化判定；None 表示允许继续。"""

    kind: AgentErrorKind
    message: str
    cost_usd: float | None = None


def merge_usage(*usages: dict | None) -> dict:
    """合并多个 usage dict（prompt/completion/total 累加）；全空返回空 dict。"""
    merged: dict = {}
    for usage in usages:
        if not usage:
            continue
        for key, value in usage.items():
            merged[key] = merged.get(key, 0) + value
    return merged


def evaluate_guard(
    *,
    cancel_event: asyncio.Event | None,
    deadline: float | None,
    cost_limiter: CostLimiterPort | None,
    running_usage: dict,
    cancelled: bool = False,
    deadline_exceeded: bool = False,
    context_error: ContextWindowExceededError | None = None,
) -> GuardResult | None:
    """按固定优先级判定取消、绝对期限、累计成本与上下文超限。

    优先级是 CANCELLED > TIMEOUT > COST_EXCEEDED > CONTEXT_EXCEEDED。最终请求
    上下文是否可容纳只有 Integration 掌握；策略捕获其类型化异常后通过
    context_error 参与同一次判定。

    running_usage 由调用方按策略累计口径现算，本函数不修改它。cancelled 与
    deadline_exceeded 用于把 LLM Facade 已判定的类型化终止信号纳入同一决策。
    """
    if cancelled or (cancel_event is not None and cancel_event.is_set()):
        return GuardResult(AgentErrorKind.CANCELLED, "用户取消")
    if deadline_exceeded or (deadline is not None and time.monotonic() >= deadline):
        return GuardResult(AgentErrorKind.TIMEOUT, "执行超时")
    if cost_limiter is not None:
        exceeded, cost = cost_limiter.check(running_usage)
        if exceeded:
            return GuardResult(
                AgentErrorKind.COST_EXCEEDED,
                f"成本超限（累计 ${cost}）",
                cost_usd=cost,
            )
    if context_error is not None:
        return GuardResult(AgentErrorKind.CONTEXT_EXCEEDED, str(context_error))
    return None


async def dispatch_error(
    error_handlers: ErrorHandlerRegistry,  # 调用方已 resolve 的 registry
    kind: AgentErrorKind,
    message: str,
    iteration: int = 0,
) -> AgentErrorAction:
    """错误分发统一入口：RAISE 决策抛 AgentRunError，否则返回 action。

    收敛 registry.dispatch + context 构造 + RAISE 抛的机械段（react/reflection/
    planner 三策略共用）；error_handlers 由调用方传入（各策略持有其生命周期）。
    """
    action = await error_handlers.dispatch(
        kind, AgentErrorContext(kind=kind, message=message, iteration=iteration)
    )
    if action == AgentErrorAction.RAISE:
        raise AgentRunError(kind, message, iteration)
    return action
