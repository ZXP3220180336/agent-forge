"""写适配器的隔离验证；正式网关的 B 门禁另由 enablement 测试覆盖。"""

import asyncio
import builtins
import io
import os
import threading
from pathlib import Path
from typing import Any

import aiofiles.threadpool
import pytest

from app.integration.tools.admission import ToolAdmission
from app.integration.tools.builtin import file_ops
from app.integration.tools.builtin.file_ops import WriteFileTool
from app.integration.tools.execution import (
    ToolAttemptTimeoutError,
    ToolEffectClass,
    ToolExecutionSettings,
    ToolExecutionSupervisor,
)
from app.shared.exceptions import ToolCancelledError
from tests.tool_lifecycle import execution_kwargs


async def _until(predicate: Any) -> None:
    """有界等候可观察状态，不靠固定睡眠制造线程竞态。"""
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("phase", ["mkdir", "open", "write", "close"])
@pytest.mark.parametrize("termination", ["cancel", "timeout", "hard"])
async def test_write_worker_retains_permit_and_lock_until_finished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    termination: str,
) -> None:
    """取消/超时不得宣称同步写已结束；第二次同文件写不能越过在途串行锁。"""
    tool = WriteFileTool()
    monkeypatch.setattr(tool, "_allowed_dirs", (os.path.normcase(str(tmp_path)),))
    target = tmp_path / "nested" / "out.txt"
    entered, release, worker_returned = threading.Event(), threading.Event(), threading.Event()
    real_open, real_makedirs = builtins.open, os.makedirs
    calls = 0
    opened = []

    def pause_first() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5), "测试线程未被有界释放"

    class DelayedFile(io.TextIOWrapper):
        """保留真实文件 I/O，只在指定阶段用事件控制调度。"""

        def write(self, text: str) -> int:
            if phase == "write":
                pause_first()
            return super().write(text)

        def close(self) -> None:
            if phase == "close" and not self.closed:
                pause_first()
            super().close()

    def delayed_open(*args: Any, **kwargs: Any) -> Any:
        if phase == "open":
            pause_first()
        result = real_open(*args, **kwargs)
        if phase in ("write", "close"):
            result = DelayedFile(result.detach(), encoding="utf-8")
        opened.append(result)
        worker_returned.set()
        return result

    def delayed_makedirs(*args: Any, **kwargs: Any) -> None:
        if phase == "mkdir":
            pause_first()
        real_makedirs(*args, **kwargs)
        worker_returned.set()

    monkeypatch.setattr(file_ops, "open", delayed_open, raising=False)
    # 同一竞态也能在修复前 aiofiles 线程入口复现，不改变真实文件写语义。
    monkeypatch.setattr(aiofiles.threadpool, "sync_open", delayed_open)
    monkeypatch.setattr(file_ops.os, "makedirs", delayed_makedirs)
    admission = ToolAdmission(global_limit=2, per_run_limit=2)
    supervisor = ToolExecutionSupervisor(
        ToolExecutionSettings(cleanup_timeout_seconds=0.02, shutdown_timeout_seconds=0.02),
        max_workers=2,
    )
    serial = asyncio.Lock()
    first_call = execution_kwargs()["call"]
    handles = []

    async def start_write(call: Any, content: str) -> Any:
        owned = False

        async def invoke(handle: Any) -> Any:
            nonlocal owned
            await serial.acquire()
            owned = True
            return await tool.invoke({"file_path": str(target), "content": content}, handle)

        def unlock() -> None:
            if owned:
                serial.release()

        handle = supervisor.start(
            invoke,
            call=call,
            permit=await admission.acquire(call),
            owner=tool,
            on_release=unlock,
        )
        handles.append(handle)
        return handle

    first = await start_write(first_call, "first")
    waiting = asyncio.create_task(supervisor.wait(first, timeout=0.1 if termination == "timeout" else None))
    try:
        await _until(entered.is_set)
        if termination == "cancel":
            first_call.cancel_events[0].set()
        elif termination == "hard":
            waiting.cancel()
        expected = {
            "cancel": ToolCancelledError,
            "timeout": ToolAttemptTimeoutError,
            "hard": asyncio.CancelledError,
        }[termination]
        with pytest.raises(expected):
            await waiting
        assert not first.completed
        assert first.transferred
        assert admission.active == 1
        assert serial.locked()
        assert supervisor.owns(tool)
        second = await start_write(execution_kwargs()["call"], "second")
        await asyncio.sleep(0)
        assert calls == 1
        assert not second.completed
        assert admission.active == 2
        release.set()
        result = await supervisor.wait(second, timeout=2)
        assert result.success
        assert first.completed
        assert target.read_text(encoding="utf-8") == "second"
        assert admission.active == 0
        assert not serial.locked()
        assert first.thread_results[0].success
        assert all(file.closed for file in opened)
    finally:
        release.set()
        await asyncio.gather(waiting, return_exceptions=True)
        await _until(worker_returned.is_set)
        await _until(lambda: all(handle.completed for handle in handles))
        for file in opened:
            file.close()
        await supervisor.close()


def test_write_effect_is_explicit() -> None:
    """写入能力必须明确为 MAY_WRITE，不能继承未知或只读声明。"""
    assert WriteFileTool().describe_execution({}).effect_class == ToolEffectClass.MAY_WRITE


async def test_standalone_write_keeps_validation_and_closes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独立适配器入口保留验证/路径策略及中文写入；不是正式网关启用证据。"""
    tool = WriteFileTool()
    monkeypatch.setattr(tool, "_allowed_dirs", (os.path.normcase(str(tmp_path)),))
    target = tmp_path / "nested" / "content.txt"
    assert (await tool.execute(file_path=str(target), content="中文内容")).success
    assert target.read_text(encoding="utf-8") == "中文内容"
    target.unlink()
    assert not (await tool.execute(file_path=str(target))).success
    outside = tmp_path.parent / "outside.txt"
    assert not (await tool.execute(file_path=str(outside), content="no")).success
