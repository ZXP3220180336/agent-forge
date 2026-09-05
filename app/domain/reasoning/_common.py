"""reasoning 策略共享小工具（react/reflection/planner 共用，不 import 任何策略 → 无环）。

仅收无状态纯函数与常量，不维护任何生命周期/全局可变状态（error_handlers、策略实例态等
由各策略自行持有）。命名带 `_` 前缀 = 层内私有共享（非对外导出）。
"""

import asyncio
import time

from app.domain.ports.cost_limiter import CostLimiterPort
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)


def merge_usage(*usages: dict | None) -> dict:
    """合并多个 usage dict（prompt/completion/total 累加）；全空返回空 dict。"""
    merged: dict = {}
    for usage in usages:
        if not usage:
            continue
        for key, value in usage.items():
            merged[key] = merged.get(key, 0) + value
    return merged


def guard_exceeded(
    cancel_event: asyncio.Event | None,
    start_time: float,
    max_execution_time: float | None,
    cost_limiter: CostLimiterPort | None,
    running_usage: dict,
) -> tuple[str, str]:
    """阶段/付费调用前护栏：返回 (终止原因, 成本超限原因)，均空串 = 可继续。

    终止（取消 / 总时长超限）优先于成本检查；成本 = cost_limiter.check(running_usage)
    超限（cost_limiter 注入时）。running_usage 由调用方按策略累计口径现算（如 planner 的
    react 各步 + 结构化全阶段；reflection 的 react 收集 + 自查/修正累计）。planner
    三阶段入口与 reflection 自查循环共用。
    """
    if cancel_event is not None and cancel_event.is_set():
        return "用户取消", ""
    if (
        max_execution_time is not None
        and time.monotonic() - start_time > max_execution_time
    ):
        return "执行超时", ""
    if cost_limiter is not None:
        exceeded, cost = cost_limiter.check(running_usage)
        if exceeded:
            return "", f"成本超限（累计 ${cost}）"
    return "", ""


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
