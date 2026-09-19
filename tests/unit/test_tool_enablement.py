"""未完成持久保护的工具在模型导出与真实调用前关闭准入。"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.ports.tool_execution import ToolCleanupState, ToolEffectState, ToolExecutionState
from app.domain.ports.tool_gateway import ErrorCode, ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.execution import ToolEffectClass, ToolExecutionSpec
from app.integration.tools.tool_service import ToolService
from app.shared.exceptions import ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError
from tests.tool_lifecycle import execution_kwargs


class _DeclaredTool(BaseTool):
    """具备可变可信声明的工具，记录真正执行次数。"""

    def __init__(self, spec: ToolExecutionSpec) -> None:
        self.spec = spec
        self.calls = 0
        self.approval = False
        self.change_after_call = False

    @property
    def name(self) -> str:
        return "declared"

    @property
    def description(self) -> str:
        return "测试准入"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"mode": {"type": "string"}}}

    @property
    def requires_approval(self) -> bool:
        return self.approval

    @property
    def concurrency_safe(self) -> bool:
        return False

    def describe_execution(self, parameters: dict[str, Any]) -> ToolExecutionSpec:
        return self.spec

    def can_retry(self, result_or_error: ToolResult | BaseException) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.calls += 1
        if self.change_after_call:
            self.spec = ToolExecutionSpec(effect_class=ToolEffectClass.MAY_WRITE)
            return ToolResult(False, "", error="暂态失败")
        return ToolResult(True, "ok")


_BLOCKED = [
    ToolExecutionSpec(),
    ToolExecutionSpec(effect_class=ToolEffectClass.MAY_WRITE),
    ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY, audit_required=True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", _BLOCKED)
async def test_blocked_capabilities_never_approve_acquire_or_invoke(spec: ToolExecutionSpec) -> None:
    """拒绝发生在审批和 Permit 之前，事实准确表示未执行。"""
    service = ToolService()
    tool = _DeclaredTool(spec)
    tool.approval = True
    service.register(tool)
    context = execution_kwargs()
    with (
        patch.object(
            service._executor,
            "_request_approval",
            new_callable=AsyncMock,
            return_value=(False, ToolCleanupState.NOT_NEEDED),
        ) as approve,
        patch.object(service._executor._admission, "acquire", new_callable=AsyncMock) as acquire,
    ):
        result = await service.execute(tool.name, {}, **context)
    assert result.error_code == ErrorCode.REJECTED
    assert result.retry_count == 0
    assert tool.calls == 0
    approve.assert_not_awaited()
    acquire.assert_not_awaited()
    facts = context["facts"].snapshot()
    assert len(facts) == 1
    assert facts[0].execution_state == ToolExecutionState.NOT_STARTED
    assert facts[0].effect_state == ToolEffectState.NONE
    assert facts[0].cleanup_state == ToolCleanupState.NOT_NEEDED
    await service.shutdown()


@pytest.mark.asyncio
async def test_registered_declaration_changes_are_rechecked() -> None:
    """注册时只读不代表未来调用仍有准入资格。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    assert (await service.execute(tool.name, {}, **execution_kwargs())).success
    tool.spec = ToolExecutionSpec()
    result = await service.execute(tool.name, {}, **execution_kwargs())
    assert result.error_code == ErrorCode.REJECTED
    assert tool.calls == 1
    await service.shutdown()


@pytest.mark.asyncio
async def test_capability_changed_during_approval_is_rejected_before_attempt() -> None:
    """审批期间失去只读声明时，不因已批准就启动真实工作。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    tool.approval = True
    service.register(tool)

    async def approve(*args: Any, **kwargs: Any) -> tuple[bool, ToolCleanupState]:
        tool.spec = ToolExecutionSpec()
        return True, ToolCleanupState.NOT_NEEDED

    with (
        patch.object(service._executor, "_request_approval", side_effect=approve),
        patch.object(service._executor._admission, "acquire", new_callable=AsyncMock) as acquire,
    ):
        result = await service.execute(tool.name, {}, **execution_kwargs())
    assert result.error_code == ErrorCode.REJECTED
    assert result.retry_count == 0
    assert tool.calls == 0
    acquire.assert_not_awaited()
    await service.shutdown()


@pytest.mark.asyncio
async def test_invalid_parameters_keep_validation_precedence() -> None:
    """能力门禁位于参数解析及验证之后，既有错误归因不变。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec())
    service.register(tool)
    result = await service.execute(tool.name, {"mode": 1}, **execution_kwargs())
    assert result.error_code == ErrorCode.VALIDATION
    assert tool.calls == 0
    await service.shutdown()


@pytest.mark.asyncio
async def test_capability_changed_while_waiting_for_serial_lock_is_rejected() -> None:
    """已持 Permit 的尝试等锁时声明变化，真实调用前拒绝并归还两种资源。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    lock = service._executor._tool_lock(tool.name)
    await lock.acquire()
    waiting = asyncio.Event()
    original_acquire = lock.acquire

    async def acquire() -> bool:
        waiting.set()
        return await original_acquire()

    context = execution_kwargs()
    with patch.object(lock, "acquire", side_effect=acquire):
        task = asyncio.create_task(service.execute(tool.name, {}, **context))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        tool.spec = ToolExecutionSpec(effect_class=ToolEffectClass.MAY_WRITE)
        lock.release()
        result = await asyncio.wait_for(task, timeout=1)
    assert result.error_code == ErrorCode.REJECTED
    assert result.retry_count == 0
    assert tool.calls == 0
    assert not lock.locked()
    assert service._executor._admission._active == 0
    for fact in context["facts"].snapshot():
        assert fact.execution_state == ToolExecutionState.NOT_STARTED
        assert fact.effect_state == ToolEffectState.NONE
        assert fact.cleanup_state == ToolCleanupState.NOT_NEEDED
    await service.shutdown()


@pytest.mark.asyncio
async def test_retry_rechecks_capability_without_consuming_another_permit() -> None:
    """重试前声明变更会关闭后续尝试，同时保留已经发生的事实。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    tool.change_after_call = True
    service.register(tool)
    context = execution_kwargs()
    with patch.object(service._executor._admission, "acquire", wraps=service._executor._admission.acquire) as acquire:
        result = await service.execute(tool.name, {}, max_retries=3, retry_delay=0, **context)
    assert result.error_code == ErrorCode.REJECTED
    assert result.retry_count == 1
    assert tool.calls == 1
    assert acquire.await_count == 1
    assert any(fact.attempt_id is not None for fact in context["facts"].snapshot())
    await service.shutdown()


@pytest.mark.parametrize("method", ["get_openai_tools", "get_openai_responses"])
@pytest.mark.parametrize("spec", _BLOCKED)
def test_exports_hide_unavailable_tools_and_recheck_changes(method: str, spec: ToolExecutionSpec) -> None:
    """两种协议只导出当前可执行工具，不缓存注册时的声明。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    assert len(getattr(service, method)()) == 1
    tool.spec = spec
    assert getattr(service, method)() == []


@pytest.mark.parametrize("method", ["get_openai_tools", "get_openai_responses"])
def test_exports_do_not_assume_missing_parameters_are_read_only(method: str) -> None:
    """依赖实际参数才能声明的工具不能在缺少参数时推定可用。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    with patch.object(tool, "describe_execution", side_effect=KeyError("mode")):
        assert getattr(service, method)() == []


_BAD_DECLARATIONS = [
    RuntimeError("声明失败"),
    None,
    {},
    SimpleNamespace(
        effect_class=ToolEffectClass.READ_ONLY,
        audit_required=False,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("bad", _BAD_DECLARATIONS)
async def test_invalid_declaration_isolated_at_every_checkpoint(stage: int, bad: Any) -> None:
    """坏声明不执行、不污染运行，归还容量及锁后同运行可继续调用。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    context = execution_kwargs()
    try:
        with patch.object(tool, "describe_execution", side_effect=[tool.spec] * (stage - 1) + [bad]):
            result = await service.execute(tool.name, {}, **context)
        assert result.error_code == ErrorCode.REJECTED
        assert result.retry_count == 0
        assert tool.calls == 0
        assert not context["call"].run_stop.is_set()
        assert service._executor._admission._active == 0
        assert not service._executor._tool_lock(tool.name).locked()
        facts = context["facts"].snapshot()
        assert facts
        for fact in facts:
            assert fact.execution_state == ToolExecutionState.NOT_STARTED
            assert fact.effect_state == ToolEffectState.NONE
            assert fact.cleanup_state == ToolCleanupState.NOT_NEEDED
        next_context = execution_kwargs()
        next_context["call"] = replace(
            next_context["call"], run_id=context["call"].run_id, run_stop=context["call"].run_stop
        )
        assert (await service.execute(tool.name, {}, **next_context)).success
    finally:
        await service.shutdown()


@pytest.mark.parametrize("method", ["get_openai_tools", "get_openai_responses"])
@pytest.mark.parametrize("bad", _BAD_DECLARATIONS)
def test_exports_isolate_invalid_declarations(method: str, bad: Any) -> None:
    """非法声明不能中断整个模型工具导出。"""

    class HealthyTool(_DeclaredTool):
        @property
        def name(self) -> str:
            return "healthy"

    service = ToolService()
    healthy = HealthyTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(healthy)
    expected = getattr(service, method)()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    with patch.object(tool, "describe_execution", side_effect=[bad]):
        assert getattr(service, method)() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize(
    "error_type", [asyncio.CancelledError, ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError]
)
async def test_declaration_control_signals_propagate(stage: int, error_type: type[BaseException]) -> None:
    """故障隔离不得将取消、期限与停止信号转换成普通门禁拒绝。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    error = error_type() if error_type is asyncio.CancelledError else error_type(run_id="run", operation_id="operation")
    try:
        with (
            patch.object(tool, "describe_execution", side_effect=[tool.spec] * (stage - 1) + [error]),
            pytest.raises(error_type),
        ):
            await service.execute(tool.name, {}, **execution_kwargs())
        assert tool.calls == 0
        assert service._executor._admission._active == 0
        assert not service._executor._tool_lock(tool.name).locked()
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_declaration_wrong_signature_is_rejected() -> None:
    """实际签名不匹配产生的 TypeError 也在声明边界收敛。"""
    service = ToolService()
    tool = _DeclaredTool(ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY))
    service.register(tool)
    try:
        with patch.object(tool, "describe_execution", new=lambda: tool.spec):
            result = await service.execute(tool.name, {}, **execution_kwargs())
        assert result.error_code == ErrorCode.REJECTED
        assert tool.calls == 0
    finally:
        await service.shutdown()
