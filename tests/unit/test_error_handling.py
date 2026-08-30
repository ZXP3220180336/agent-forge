"""app/shared/error_handling.py ErrorHandlerRegistry 单元测试。"""

import pytest

from app.shared.error_handling import (
    AgentRunError,
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    ErrorHandlerRegistry,
)
from app.shared.exceptions import AppError


@pytest.mark.asyncio
async def test_default_actions():
    """未注册 handler 时用默认 action：终结性 STOP / 可恢复 CONTINUE。"""
    reg = ErrorHandlerRegistry()
    # 终结性
    assert (
        await reg.dispatch(
            AgentErrorKind.LLM_FAILED,
            AgentErrorContext(AgentErrorKind.LLM_FAILED, "x"),
        )
        == AgentErrorAction.STOP
    )
    assert (
        await reg.dispatch(
            AgentErrorKind.MAX_TURNS,
            AgentErrorContext(AgentErrorKind.MAX_TURNS, "x"),
        )
        == AgentErrorAction.STOP
    )
    assert (
        await reg.dispatch(
            AgentErrorKind.TIMEOUT,
            AgentErrorContext(AgentErrorKind.TIMEOUT, "x"),
        )
        == AgentErrorAction.STOP
    )
    assert (
        await reg.dispatch(
            AgentErrorKind.COST_EXCEEDED,
            AgentErrorContext(AgentErrorKind.COST_EXCEEDED, "x"),
        )
        == AgentErrorAction.STOP
    )
    assert (
        await reg.dispatch(
            AgentErrorKind.STALLED,
            AgentErrorContext(AgentErrorKind.STALLED, "x"),
        )
        == AgentErrorAction.STOP
    )
    assert (
        await reg.dispatch(
            AgentErrorKind.REFUSED,
            AgentErrorContext(AgentErrorKind.REFUSED, "x"),
        )
        == AgentErrorAction.STOP
    )
    assert (
        await reg.dispatch(
            AgentErrorKind.CANCELLED,
            AgentErrorContext(AgentErrorKind.CANCELLED, "x"),
        )
        == AgentErrorAction.STOP
    )
    assert (
        await reg.dispatch(
            AgentErrorKind.UNKNOWN,
            AgentErrorContext(AgentErrorKind.UNKNOWN, "x"),
        )
        == AgentErrorAction.STOP
    )
    # 空输出默认 CONTINUE（重试）
    assert (
        await reg.dispatch(
            AgentErrorKind.EMPTY_OUTPUT,
            AgentErrorContext(AgentErrorKind.EMPTY_OUTPUT, "x"),
        )
        == AgentErrorAction.CONTINUE
    )
    # 可恢复默认 CONTINUE（回喂继续）
    for kind in (
        AgentErrorKind.TOOL_FAILED,
        AgentErrorKind.PARSE_FAILED,
        AgentErrorKind.STRUCTURED_INVALID,
    ):
        assert (
            await reg.dispatch(kind, AgentErrorContext(kind, "x"))
            == AgentErrorAction.CONTINUE
        )


@pytest.mark.asyncio
async def test_register_overrides_default():
    """注册 handler 覆盖默认 action；dispatch 调用 handler 并透传 ctx。"""
    calls: list[AgentErrorContext] = []

    async def my_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        calls.append(ctx)
        return AgentErrorAction.RAISE

    reg = ErrorHandlerRegistry()
    reg.register(AgentErrorKind.LLM_FAILED, my_handler)
    assert reg.registered() == [AgentErrorKind.LLM_FAILED]

    ctx = AgentErrorContext(AgentErrorKind.LLM_FAILED, "boom", iteration=2)
    action = await reg.dispatch(AgentErrorKind.LLM_FAILED, ctx)
    assert action == AgentErrorAction.RAISE
    assert calls == [ctx]

    # 其他 kind 仍走默认
    assert (
        await reg.dispatch(
            AgentErrorKind.EMPTY_OUTPUT,
            AgentErrorContext(AgentErrorKind.EMPTY_OUTPUT, "x"),
        )
        == AgentErrorAction.CONTINUE
    )


def test_agent_error_fields():
    """AgentRunError 携带 kind / message / iteration，且纳入统一异常树（AppError）。"""
    e = AgentRunError(AgentErrorKind.TIMEOUT, "超时", iteration=3)
    assert e.kind == AgentErrorKind.TIMEOUT
    assert e.message == "超时"
    assert e.iteration == 3
    assert isinstance(e, Exception)
    # 纳入统一异常树：可被 except AppError 批量捕获
    assert isinstance(e, AppError)
