"""reasoning 策略共享小工具（react/reflection/planner 共用，不 import 任何策略 → 无环）。

仅收无状态纯函数与常量，不维护任何生命周期/全局可变状态（error_handlers、策略实例态等
由各策略自行持有）。命名带 `_` 前缀 = 层内私有共享（非对外导出）。
"""

import asyncio
import time

from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)

# 结构化最终答案工具名（Final Answer 模式，SMOL / OpenAI 官方）：模型最后调用提交
# schema 约束的结构化结果并终止循环。注入工具（非注册工具），react 识别 / reflection
# 从证据链剔除均引用此常量。
_FINAL_ANSWER_TOOL = "final_answer"


def merge_usage(*usages: dict | None) -> dict:
    """合并多个 usage dict（prompt/completion/total 累加）；全空返回空 dict。"""
    merged: dict = {}
    for usage in usages:
        if not usage:
            continue
        for key, value in usage.items():
            merged[key] = merged.get(key, 0) + value
    return merged


def should_abort(
    cancel_event: asyncio.Event | None,
    start_time: float,
    max_execution_time: float | None,
) -> tuple[bool, str]:
    """循环终止检查：用户取消 / 总时长超限。返回 (是否终止, 原因)。

    reflection 自查/修正循环与 planner 规划/执行/汇总各阶段入口共用。
    """
    if cancel_event is not None and cancel_event.is_set():
        return True, "用户取消"
    if (
        max_execution_time is not None
        and time.monotonic() - start_time > max_execution_time
    ):
        return True, "执行超时"
    return False, ""


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
