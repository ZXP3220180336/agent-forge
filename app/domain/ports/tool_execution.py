"""工具执行生命周期的领域契约。

本模块只定义跨 Domain / Integration 边界传递的身份、控制信号和事实快照；
不包含工具调度、持久化或 SDK 类型。
"""

from __future__ import annotations

import asyncio
import copy
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.domain.ports.tool_gateway import ToolResult


class ToolExecutionState(StrEnum):
    """真实执行的可证明状态。"""

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ToolEffectState(StrEnum):
    """外部效果的可证明状态。"""

    NONE = "NONE"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class ToolCleanupState(StrEnum):
    """清理责任的当前状态。"""

    NOT_NEEDED = "NOT_NEEDED"
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    TRANSFERRED = "TRANSFERRED"


def _require_identity(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")


@dataclass(frozen=True)
class ToolCallContext:
    """一次领域工具调用的稳定身份与执行控制。"""

    run_id: str  # 所属 Agent 运行身份
    batch_id: str  # 所属的一批工具调用
    tool_call_id: str  # 对应模型发出的工具调用 ID
    operation_id: str  # 一次规范业务操作身份，重试共享该身份
    cancel_events: tuple[asyncio.Event, ...]  # 多个取消来源组成的元组，例如本运行取消、父运行取消
    run_stop: asyncio.Event  # 运行已经停止继续启动新业务调用
    workflow_id: str | None = None
    deadline: float | None = None  # 业务执行绝对期限
    cleanup_deadline: float | None = None  # 清理阶段的边界

    def __post_init__(self) -> None:
        for name in ("run_id", "batch_id", "tool_call_id", "operation_id"):
            _require_identity(name, getattr(self, name))
        if self.workflow_id is not None:
            _require_identity("workflow_id", self.workflow_id)
        for name in ("deadline", "cleanup_deadline"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限 monotonic 时刻")
        if not isinstance(self.cancel_events, tuple) or any(
            not isinstance(event, asyncio.Event) for event in self.cancel_events
        ):
            raise TypeError("cancel_events 必须是 asyncio.Event 元组")
        if not isinstance(self.run_stop, asyncio.Event):
            raise TypeError("run_stop 必须是 asyncio.Event")


@dataclass(frozen=True)
class ToolFact:
    """工具事实的不可变外壳；result 由发布者和收集器分别复制接管。"""

    operation_id: str
    run_id: str
    batch_id: str
    tool_call_id: str
    revision: int
    execution_state: ToolExecutionState
    effect_state: ToolEffectState
    cleanup_state: ToolCleanupState
    attempt_id: str | None = None
    result: ToolResult | None = None
    diagnostic_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("operation_id", "run_id", "batch_id", "tool_call_id"):
            _require_identity(name, getattr(self, name))
        if self.attempt_id is not None:
            _require_identity("attempt_id", self.attempt_id)
        if self.diagnostic_id is not None:
            _require_identity("diagnostic_id", self.diagnostic_id)
        if self.revision < 0:
            raise ValueError("revision 不能为负数")
        if self.result is not None:
            object.__setattr__(self, "result", copy.deepcopy(self.result))


class ToolFactSink(Protocol):
    """同步事实入口；实现不得执行 I/O 或用户回调。"""

    def record(self, fact: ToolFact) -> bool:
        """已接管该版本或更新版本返回 True；关闭等未接管情形返回 False。"""
        ...
