"""Reasoning 策略共享的不可变执行参数值对象。

这些对象按变化原因分组，避免三种策略的 ``execute`` 入口持续扩张为无组织的
标量参数列表。对象本身只承载配置与运行控制引用，不拥有策略状态或生命周期。
``frozen=True`` 固定字段引用；``asyncio.Event`` 仍可由其既有 Owner 设置。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReasoningRunScope:
    """一次推理运行的身份、完成信号和取消传播链。"""

    run_id: str
    run_stop: asyncio.Event
    workflow_id: str | None = None
    parent_cancel_events: tuple[asyncio.Event, ...] = ()
    cancel_event: asyncio.Event | None = None

    def __post_init__(self) -> None:
        """在任何策略副作用前拒绝无效运行身份和控制信号。"""
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("ReasoningRunScope.run_id 必须是非空字符串")
        if not isinstance(self.run_stop, asyncio.Event):
            raise TypeError("ReasoningRunScope.run_stop 必须是 asyncio.Event")
        if self.workflow_id is not None and (not isinstance(self.workflow_id, str) or not self.workflow_id.strip()):
            raise ValueError("ReasoningRunScope.workflow_id 必须是非空字符串或 None")
        if not isinstance(self.parent_cancel_events, tuple) or any(
            not isinstance(event, asyncio.Event) for event in self.parent_cancel_events
        ):
            raise TypeError("ReasoningRunScope.parent_cancel_events 必须是 asyncio.Event 元组")
        if self.cancel_event is not None and not isinstance(self.cancel_event, asyncio.Event):
            raise TypeError("ReasoningRunScope.cancel_event 必须是 asyncio.Event 或 None")


@dataclass(frozen=True, slots=True)
class ModelOptions:
    """策略调用模型时共享的采样与输出参数。"""

    temperature: float = 0.2
    max_tokens: int = 4096


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """推理循环的轮次、墙钟、重复动作上限与批次收尾窗口。"""

    max_iterations: int = 10
    max_execution_time: float | None = None
    max_same_action_turns: int = 3
    # 批次首个控制异常后给在途兄弟的有界收尾窗口；默认值与配置规格一致，生产值由装配根注入。
    batch_cleanup_grace: float = 1.0

    def __post_init__(self) -> None:
        """拒绝会让主循环零执行或让收尾窗口失效的非法上限。

        ``max_iterations`` 与 ``max_same_action_turns`` 的下界取自配置规格（1-100 与
        >= 1）：``<= 0`` 时对应循环一次都不执行，主循环直接落到循环之后的
        ``_finalize_max_turns``，产出「已达到最大迭代次数(N)」的正常终态并把 N 带进
        ``AgentResult.iterations``——非法配置被伪装成业务结果。此处只收紧下界，不设
        上界：``<= 100`` 是配置策略上限，直接构造策略仍可合法跑更多轮。

        ``max_execution_time`` 不在此校验：配置侧没有对应口径，而 ``_resolve_deadlines``
        已按 ``max(0.0, ...)`` 处理负值并在 docstring 声明该语义，收紧它会改变既有定义
        的行为。
        """
        if self.max_iterations < 1:
            raise ValueError("ExecutionLimits.max_iterations 必须 >= 1")
        if self.max_same_action_turns < 1:
            raise ValueError("ExecutionLimits.max_same_action_turns 必须 >= 1")
        if not math.isfinite(self.batch_cleanup_grace) or self.batch_cleanup_grace <= 0:
            raise ValueError("ExecutionLimits.batch_cleanup_grace 必须是有限正数")


@dataclass(frozen=True, slots=True)
class ContextWindowLimits:
    """进入模型调用前的上下文窗口限制。"""

    max_rounds: int | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class RecoveryBudget:
    """推理流程中重试、重规划和修订的次数预算。

    ``max_replan_rounds`` / ``max_refine_rounds`` 为 ``None`` 时表示当前
    策略不适用该预算；``0`` 表示预算适用但不允许对应恢复动作。
    """

    max_empty_retries: int = 2
    max_llm_fail_retries: int = 2
    max_tool_protocol_retries: int = 2
    max_replan_rounds: int | None = None
    max_refine_rounds: int | None = None

    def __post_init__(self) -> None:
        """拒绝未定义的负预算；None 只用于策略不适用的可选预算。"""
        for field_name in (
            "max_empty_retries",
            "max_llm_fail_retries",
            "max_tool_protocol_retries",
            "max_replan_rounds",
            "max_refine_rounds",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"RecoveryBudget.{field_name} 不能为负")


@dataclass(frozen=True, slots=True)
class ToolExecutionOptions:
    """策略发起工具调用时传给工具网关的执行限制。"""

    timeout: int | None = None
    max_attempts: int | None = None
