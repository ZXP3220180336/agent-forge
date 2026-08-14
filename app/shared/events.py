# ============================================
# core/events.py - Agent/LLM 层共享 SSE 事件定义
# ============================================
"""
统一 SSE 事件类型与构建函数
============================

LLM 层（llm_service.py）和 Agent 层（core/agent/）共用此模块，
确保两端产出的事件格式一致。

LLM 层产出的事件：
  - reasoning（思考 token）
  - message（回答 token）
  - error（调用失败）

Agent 层额外产出：
  - tool_call（工具调用通知）
  - tool_result（工具执行结果）
  - done（Agent 完成）
  - agent_info（状态信息）

用法：
    from app.shared.events import build_message_event, build_tool_call_event
    yield build_message_event("Hello")
    yield build_tool_call_event("search", {"q": "..."}, iteration=1)
"""

import json
from enum import Enum
from typing import Any


class AgentEventType(str, Enum):
    """Agent 事件类型枚举（所有 SSE 事件归口管理）"""

    REASONING = "reasoning"  # LLM 思考 token
    MESSAGE = "message"  # LLM 回答 token
    TOOL_CALL = "tool_call"  # 工具调用通知
    TOOL_RESULT = "tool_result"  # 工具执行结果
    DONE = "done"  # Agent 完成
    ERROR = "error"  # 异常
    INFO = "agent_info"  # Agent 状态信息


def build_sse_event(event_type: str | AgentEventType, content: Any, **extra) -> str:
    """
    构建标准 SSE 事件字符串。

    Args:
        event_type: 事件类型（字符串或枚举）
        content: 事件内容
        **extra: 额外字段（如 iteration、tool 等）

    Returns:
        "data: {json}\n\n"
    """
    data = {
        "type": event_type.value
        if isinstance(event_type, AgentEventType)
        else event_type,
        "content": content,
    }
    data.update(extra)
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ===== 便捷构造器（每个事件类型一个函数） =====


def build_reasoning_event(content: str) -> str:
    """思考过程 token 事件"""
    return build_sse_event(AgentEventType.REASONING, content)


def build_message_event(content: str) -> str:
    """回答内容 token 事件"""
    return build_sse_event(AgentEventType.MESSAGE, content)


def build_tool_call_event(tool: str, params: dict, iteration: int) -> str:
    """工具调用事件"""
    return build_sse_event(
        AgentEventType.TOOL_CALL,
        tool,
        params=params,
        iteration=iteration,
    )


def build_tool_result_event(
    tool: str,
    result: str,
    duration: float,
    iteration: int,
) -> str:
    """工具执行结果事件"""
    return build_sse_event(
        AgentEventType.TOOL_RESULT,
        result,
        tool=tool,
        duration=round(duration, 3),
        iteration=iteration,
    )


def build_error_event(content: str) -> str:
    """错误事件"""
    return build_sse_event(AgentEventType.ERROR, content)


def build_info_event(content: str) -> str:
    """Agent 状态信息事件"""
    return build_sse_event(AgentEventType.INFO, content)


def build_done_event(iterations: int, total_tokens: int = 0) -> str:
    """完成事件"""
    return build_sse_event(
        AgentEventType.DONE,
        "",
        iterations=iterations,
        total_tokens=total_tokens,
    )
