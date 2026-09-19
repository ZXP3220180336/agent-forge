"""真实 Executor → Supervisor → 线程/协程边界，不以取消包装 Future 代替完成。"""

import asyncio
import threading
import weakref
from dataclasses import replace
from typing import ClassVar

import pytest

from app.domain.ports.tool_execution import ToolCleanupState, ToolExecutionState
from app.domain.ports.tool_gateway import ErrorCode, ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.execution import (
    ToolEffectClass,
    ToolExecutionSettings,
    ToolExecutionSpec,
    ToolShutdownIncompleteError,
)
from app.integration.tools.tool_service import ToolService
from app.shared.exceptions import ToolCancelledError, ToolDeadlineExceededError
from tests.tool_lifecycle import execution_kwargs


class _ProbeTool(BaseTool):
    name = "probe"
    description = "probe"
    parameters: ClassVar = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs):
        self.calls += 1
        self.entered.set()
        return ToolResult(True, "done")


async def _service(tool, *, records=4):
    service = ToolService(
        max_concurrent_tools=1,
        admission_timeout_seconds=0.02,
        execution_settings=ToolExecutionSettings(
            cleanup_timeout_seconds=0.01,
            max_recovery_records=records,
            shutdown_timeout_seconds=0.1,
        ),
    )
    service.register(tool)
    await service.refresh_external_tools()
    return service


async def _until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("termination", ["cancel", "deadline", "hard"])
async def test_real_thread_keeps_capacity_until_true_completion(termination):
    entered = threading.Event()
    release = threading.Event()

    class ThreadTool(_ProbeTool):
        async def invoke(self, parameters, execution):
            self.calls += 1
            return await execution.run_sync(self.work)

        def work(self):
            entered.set()
            assert release.wait(3)
            return ToolResult(True, "late evidence")

    tool = ThreadTool()
    service = await _service(tool)
    context = execution_kwargs()
    if termination == "deadline":
        context["call"] = replace(context["call"], deadline=asyncio.get_running_loop().time() + 0.1)
    task = asyncio.create_task(service.execute(tool.name, {}, **context))
    try:
        await _until(entered.is_set)
        if termination == "cancel":
            context["call"].cancel_events[0].set()
        elif termination == "hard":
            task.cancel()
        expected = {
            "cancel": ToolCancelledError,
            "deadline": ToolDeadlineExceededError,
            "hard": asyncio.CancelledError,
        }[termination]
        with pytest.raises(expected):
            await task
        assert service._executor._admission.active == 1
        records = service._executor._supervisor.records
        assert len(records) == 1 and records[0].transferred
        assert any(f.cleanup_state == ToolCleanupState.TRANSFERRED for f in context["facts"].snapshot())
        rejected = await service.execute(tool.name, {}, **execution_kwargs())
        assert rejected.error_code == ErrorCode.CAPACITY_EXCEEDED
        assert tool.calls == 1
    finally:
        release.set()
        await _until(lambda: service._executor._admission.active == 0)
        await service.shutdown()
    assert records[0].thread_results[0].content == "late evidence"


@pytest.mark.parametrize("termination", ["cancel", "hard", "timeout"])
@pytest.mark.parametrize("thread_fails", [False, True])
async def test_thread_outcome_during_cleanup_keeps_recovery_owner(termination, thread_fails):
    entered = threading.Event()
    release = threading.Event()

    class ThreadTool(_ProbeTool):
        async def invoke(self, parameters, execution):
            return await execution.run_sync(self.work)

        def work(self):
            entered.set()
            assert release.wait(5)
            if thread_fails:
                raise ValueError("late thread failure")
            return ToolResult(True, "valuable thread evidence")

    tool = ThreadTool()
    service = ToolService(
        max_concurrent_tools=1,
        execution_settings=ToolExecutionSettings(cleanup_timeout_seconds=2, max_recovery_records=1),
    )
    service.register(tool)
    context = execution_kwargs()
    task = asyncio.create_task(
        service.execute(
            tool.name,
            {},
            timeout=0.1 if termination == "timeout" else 5,
            **context,
        )
    )
    try:
        await _until(entered.is_set)
        handle = service._executor._supervisor.records[0]
        if termination == "cancel":
            context["call"].cancel_events[0].set()
        elif termination == "hard":
            task.cancel()
        # 先确认等待协程已取消，再让真实线程于清理窗口内结束。
        await _until(lambda: handle._task.done())
        release.set()
        if termination == "timeout":
            assert (await task).error_code == ErrorCode.TIMEOUT
        else:
            with pytest.raises(ToolCancelledError if termination == "cancel" else asyncio.CancelledError):
                await task
        assert handle.completed and not handle.transferred
        assert service._executor._admission.active == 0
        assert handle in service._executor._supervisor.records
        if thread_fails:
            assert str(handle.thread_errors[0]) == "late thread failure"
        else:
            assert handle.thread_results[0].content == "valuable thread evidence"
        # 未交付证据仍占用有限恢复条目，不通过删除历史为新调用腾位置。
        rejected = await service.execute(tool.name, {}, **execution_kwargs())
        assert rejected.error_code == ErrorCode.CAPACITY_EXCEEDED
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.shutdown()


async def test_late_success_is_owned_before_cancel_is_propagated():
    class LateTool(_ProbeTool):
        async def execute(self, **kwargs):
            self.calls += 1
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ToolResult(True, "valuable late result")

    tool = LateTool()
    service = await _service(tool)
    context = execution_kwargs()
    task = asyncio.create_task(service.execute(tool.name, {}, **context))
    await tool.entered.wait()
    context["call"].cancel_events[0].set()
    with pytest.raises(ToolCancelledError):
        await task
    facts = context["facts"].snapshot()
    assert len(facts) == 2
    assert all(f.execution_state == ToolExecutionState.SUCCEEDED for f in facts)
    assert all(f.result.content == "valuable late result" for f in facts)
    assert service._executor._admission.active == 0
    await service.shutdown()


async def test_late_success_after_local_timeout_keeps_success_fact_but_returns_timeout():
    class LateTool(_ProbeTool):
        async def execute(self, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ToolResult(True, "late")

    service = await _service(LateTool())
    context = execution_kwargs()
    result = await service.execute("probe", {}, timeout=0.01, **context)
    assert result.error_code == ErrorCode.TIMEOUT
    assert all(f.result.success for f in context["facts"].snapshot())
    await service.shutdown()


async def test_retry_backoff_releases_permit_and_cancel_stops_next_attempt():
    class RetryTool(_ProbeTool):
        async def execute(self, **kwargs):
            self.calls += 1
            return ToolResult(False, "", error="temporary")

        def can_retry(self, result_or_error):
            return True

    tool = RetryTool()
    service = await _service(tool)
    context = execution_kwargs()
    task = asyncio.create_task(service.execute("probe", {}, retry_delay=10, **context))
    await _until(lambda: tool.calls == 1 and service._executor._admission.active == 0)
    context["call"].cancel_events[0].set()
    with pytest.raises(ToolCancelledError):
        await asyncio.wait_for(task, 0.5)
    assert tool.calls == 1
    await service.shutdown()


async def test_transferred_coroutine_does_not_keep_domain_collector():
    release = asyncio.Event()

    class ResistantTool(_ProbeTool):
        async def execute(self, **kwargs):
            self.entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return ToolResult(True, "late")

    tool = ResistantTool()
    service = await _service(tool)
    context = execution_kwargs()
    collector_ref = weakref.ref(context["facts"])
    task = asyncio.create_task(service.execute("probe", {}, **context))
    await tool.entered.wait()
    context["call"].cancel_events[0].set()
    with pytest.raises(ToolCancelledError):
        await task
    del context, task
    # 已完成 task 的异常 traceback 可能在本轮事件循环尾部才释放。
    await asyncio.sleep(0)
    import gc

    gc.collect()
    try:
        assert collector_ref() is None
    finally:
        release.set()
        await _until(lambda: service._executor._admission.active == 0)
        await service.shutdown()


@pytest.mark.parametrize("kind", ["audit", "hook"])
async def test_resistant_observation_is_bounded_and_keeps_owner(kind):
    release = asyncio.Event()
    entered = asyncio.Event()

    async def observe(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    service = await _service(_ProbeTool())
    if kind == "audit":
        service._executor._auditor.record = observe
    else:
        service.add_execution_hook(observe)
    task = asyncio.create_task(service.execute("probe", {}, **execution_kwargs()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.3)
        assert task.done(), "非关键观测吞取消后不能拖住已完成业务"
        assert task.result().success
        assert any(not record.completed for record in service._executor._supervisor.records)
    finally:
        release.set()
        await task
        await _until(lambda: service._executor._admission.active == 0)
        await service.shutdown()


async def test_service_does_not_unload_dependency_while_real_thread_is_running():
    release = threading.Event()
    entered = threading.Event()

    class ThreadTool(_ProbeTool):
        unloaded = False

        async def invoke(self, parameters, execution):
            return await execution.run_sync(self.work)

        def work(self):
            entered.set()
            assert release.wait(3)
            return ToolResult(True, "finished")

        async def on_unload(self):
            self.unloaded = True

    tool = ThreadTool()
    service = await _service(tool)
    context = execution_kwargs()
    task = asyncio.create_task(service.execute("probe", {}, **context))
    try:
        await _until(entered.is_set)
        context["call"].cancel_events[0].set()
        with pytest.raises(ToolCancelledError):
            await task
        with pytest.raises(ToolShutdownIncompleteError):
            await service.shutdown()
        assert not tool.unloaded
    finally:
        release.set()
        await _until(lambda: service._executor._admission.active == 0)
        # 上次有界关闭的 Owner 可能尚在完成其退出通知。
        await _until(lambda: service._shutdown_task.done())
        await service.shutdown()
    assert tool.unloaded


async def test_cancel_during_approval_prevents_business_execution():
    entered = asyncio.Event()

    class ApprovedTool(_ProbeTool):
        requires_approval = True

    class Approval:
        async def request(self, name, parameters):
            entered.set()
            await asyncio.Event().wait()

    tool = ApprovedTool()
    service = await _service(tool)
    service._executor._approval_gate = Approval()
    context = execution_kwargs()
    task = asyncio.create_task(service.execute("probe", {}, **context))
    await entered.wait()
    context["call"].cancel_events[0].set()
    with pytest.raises(ToolCancelledError):
        await task
    assert tool.calls == 0
    assert service._executor._admission.active == 0
    assert all(f.execution_state == ToolExecutionState.NOT_STARTED for f in context["facts"].snapshot())
    await service.shutdown()


async def test_unacknowledged_result_consumes_recovery_capacity_before_next_business_call():
    tool = _ProbeTool()
    service = await _service(tool, records=1)
    context = execution_kwargs()
    context["facts"].close()
    result = await service.execute("probe", {}, **context)
    assert result.success
    assert len(service._executor._supervisor.records) == 1
    rejected = await service.execute("probe", {}, **execution_kwargs())
    assert rejected.error_code == ErrorCode.CAPACITY_EXCEEDED
    assert tool.calls == 1
    await service.shutdown()


async def test_retry_admission_rejection_preserves_actual_attempt_count():
    class RetryTool(_ProbeTool):
        async def execute(self, **kwargs):
            self.calls += 1
            return ToolResult(False, "", error="temporary")

        def can_retry(self, result_or_error):
            return True

    tool = RetryTool()
    service = await _service(tool)
    acquire = service._executor._admission.acquire
    acquisitions = 0

    async def reject_second(call, **kwargs):
        nonlocal acquisitions
        acquisitions += 1
        return await acquire(call, **kwargs) if acquisitions == 1 else None

    service._executor._admission.acquire = reject_second
    context = execution_kwargs()
    result = await service.execute("probe", {}, retry_delay=0, **context)
    assert result.error_code == ErrorCode.CAPACITY_EXCEEDED
    assert result.retry_count == tool.calls == 1
    assert any(f.attempt_id and f.execution_state == ToolExecutionState.FAILED for f in context["facts"].snapshot())
    await service.shutdown()
