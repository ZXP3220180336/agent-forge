"""离线迁移资源 Owner 的控制流验收；不代替 PostgreSQL 原子性验证。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.infrastructure.database_migrations as module


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """记录独立事务、提交、回滚与资源释放的顺序。"""
    for version in (1, 2):
        (tmp_path / f"{version:04d}_step.sql").write_bytes(b"SELECT 1;\n")
    migrations = module.load_migrations(tmp_path)
    calls = []
    driver = SimpleNamespace(closed=False)
    driver.is_closed = lambda: driver.closed

    def terminate() -> None:
        calls.append("terminate")
        driver.closed = True

    driver.terminate = terminate
    holder = SimpleNamespace(listener=None, transactions=[])

    async def start() -> None:
        calls.append("start")
        holder.listener(SimpleNamespace(driver_connection=driver), None)

    async def begin() -> Any:
        calls.append("begin")
        transaction = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
        transaction.commit.side_effect = lambda: calls.append("commit")
        transaction.rollback.side_effect = lambda: calls.append("rollback")
        holder.transactions.append(transaction)
        return transaction

    async def close() -> None:
        calls.append("close")
        driver.closed = True

    async def dispose() -> None:
        calls.append("dispose")
        driver.closed = True

    connection = SimpleNamespace(
        start=AsyncMock(side_effect=start), begin=AsyncMock(side_effect=begin), close=AsyncMock(side_effect=close)
    )
    engine = SimpleNamespace(sync_engine=object(), connect=lambda: connection, dispose=AsyncMock(side_effect=dispose))

    def listen(target: Any, name: str, callback: Any, **kwargs: Any) -> None:
        holder.listener = callback

    monkeypatch.setattr(module.event, "listen", listen)
    apply = AsyncMock(side_effect=[1, 2, None])
    monkeypatch.setattr(module, "apply_next_migration", apply)
    return SimpleNamespace(
        engine=engine,
        connection=connection,
        driver=driver,
        calls=calls,
        holder=holder,
        migrations=migrations,
        apply=apply,
        prepare=AsyncMock(return_value=None),
    )


def command(backend: SimpleNamespace, **kwargs: Any) -> module.MigrationCommand:
    """构造短预算命令，schema 检查器仅在测试内部注入。"""
    return module.MigrationCommand(
        backend.engine,
        backend.migrations,
        timeouts=module.MigrationTimeouts(
            file_timeout_seconds=0.3, total_timeout_seconds=1, cleanup_timeout_seconds=0.05
        ),
        prepare_schema=backend.prepare,
        **kwargs,
    )


def deadline() -> float:
    return asyncio.get_running_loop().time() + 1


async def test_each_file_has_its_own_commit_and_noop_rolls_back(backend: SimpleNamespace) -> None:
    owner = command(backend)
    result = await owner.run(deadline=deadline())
    assert result.reason == "ok" and result.cleanup_complete
    assert result.confirmed == [1, 2] and result.uncertain_version is None
    assert backend.calls == ["start", "begin", "commit", "begin", "commit", "begin", "rollback", "close", "dispose"]
    assert len(backend.holder.transactions) == 3
    with pytest.raises(module.MigrationError, match="internal_error"):
        # 复用一次性 Owner 是调用方缺陷，不得伪装成可恢复的事务原因码
        await owner.run(deadline=deadline())


async def test_later_failure_preserves_confirmed_prefix(backend: SimpleNamespace) -> None:
    backend.apply.side_effect = [1, module.MigrationError("schema_mismatch")]
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "schema_mismatch" and result.confirmed == [1]
    assert result.cleanup_complete
    assert backend.calls == ["start", "begin", "commit", "begin", "rollback", "close", "dispose"]


async def test_commit_response_loss_is_unknown_not_retried(backend: SimpleNamespace) -> None:
    async def prepare(*args: Any, **kwargs: Any) -> None:
        backend.holder.transactions[-1].commit.side_effect = OSError("SECRET_URL")

    backend.prepare.side_effect = prepare
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "commit_unknown" and result.uncertain_version == 1
    assert result.confirmed == []
    assert backend.apply.await_count == 1
    backend.holder.transactions[0].rollback.assert_awaited_once()


async def test_cancel_during_execution_rolls_back_before_propagation(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    backend.apply.side_effect = hang
    owner = command(backend)
    task = asyncio.create_task(owner.run(deadline=deadline()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    backend.holder.transactions[0].rollback.assert_awaited_once()
    assert owner.progress.reason == "cancelled"
    assert owner.progress.cleanup_complete


async def test_missing_gate_never_starts_connection(backend: SimpleNamespace) -> None:
    backend.prepare = None
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "schema_gate_unavailable"
    backend.connection.start.assert_not_awaited()
    backend.engine.dispose.assert_awaited_once()


async def test_baseline_is_committed_separately_from_next_file(backend: SimpleNamespace) -> None:
    backend.prepare.side_effect = [1, None, None]
    backend.apply.side_effect = [2, None]
    result = await command(backend, baseline_existing=True).run(deadline=deadline())
    assert result.confirmed == [1, 2] and result.reason == "ok"
    assert backend.apply.await_count == 2


async def test_cleanup_failure_preserves_database_reason(backend: SimpleNamespace) -> None:
    backend.apply.side_effect = module.MigrationError("permission_denied")
    backend.connection.close.side_effect = OSError("SECRET")
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "permission_denied"
    assert not result.cleanup_complete
    assert backend.driver.closed


async def test_successful_commits_survive_cleanup_hang(backend: SimpleNamespace) -> None:
    backend.connection.close.side_effect = asyncio.Event().wait
    result = await command(backend).run(deadline=deadline())
    assert result.confirmed == [1, 2]
    assert result.reason == "cleanup_incomplete" and not result.cleanup_complete
    assert backend.driver.closed


async def test_unstarted_connection_is_not_closed(backend: SimpleNamespace) -> None:
    backend.connection.start.side_effect = OSError("SECRET_URL")
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "migration_database_error"
    backend.connection.close.assert_not_awaited()
    backend.engine.dispose.assert_awaited_once()


async def test_expired_deadline_does_not_begin_database_work(backend: SimpleNamespace) -> None:
    result = await command(backend).run(deadline=asyncio.get_running_loop().time() - 1)
    assert result.reason == "timeout"
    backend.connection.start.assert_not_awaited()


async def test_unknown_exception_is_sanitized_internal_failure(backend: SimpleNamespace) -> None:
    backend.apply.side_effect = ValueError("SECRET")
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "internal_error"
    assert "SECRET" not in repr(result)


async def test_file_timeout_rolls_back_and_stops_next_file(backend: SimpleNamespace) -> None:
    async def hang(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    backend.apply.side_effect = hang
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "timeout" and result.cleanup_complete
    assert backend.apply.await_count == 1
    backend.holder.transactions[0].rollback.assert_awaited_once()


async def test_budget_exhausted_before_commit_is_timeout_not_unknown(
    backend: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交请求尚未发出时预算耗尽：报超时，不得登记未确认版本。"""
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0.0

    async def apply(*args: Any, **kwargs: Any) -> int:
        nonlocal offset
        offset = 10.0  # 确定性耗尽预算：apply 返回后期限已过，同步窗口不会被 timeout_at 覆盖
        return 1

    backend.apply.side_effect = apply
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "timeout" and result.uncertain_version is None
    assert result.confirmed == []
    # 收尾期限同样已过，故只断言保守结果；回滚时序由 file timeout 用例覆盖
    assert not result.cleanup_complete


@pytest.mark.parametrize("baseline,baseline_existing", [(1, False), (2, True), (True, True)])
async def test_unexpected_baseline_result_is_internal_failure(
    backend: SimpleNamespace, baseline: Any, baseline_existing: bool
) -> None:
    """门禁返回值不符合契约时按未知缺陷拒绝，不登记版本、不执行 SQL。"""
    backend.prepare.side_effect = [baseline]
    result = await command(backend, baseline_existing=baseline_existing).run(deadline=deadline())
    assert result.reason == "internal_error" and result.confirmed == []
    assert backend.apply.await_count == 0


async def test_expired_cleanup_deadline_reports_incomplete(backend: SimpleNamespace) -> None:
    """进入收尾时预算已耗尽：保留已确认版本，保守报告收尾未完成。"""
    owner = module.MigrationCommand(
        backend.engine,
        backend.migrations,
        timeouts=module.MigrationTimeouts(file_timeout_seconds=0.3, total_timeout_seconds=1, cleanup_timeout_seconds=0),
        prepare_schema=backend.prepare,
    )
    result = await owner.run(deadline=deadline())
    assert result.confirmed == [1, 2]
    assert result.reason == "cleanup_incomplete" and not result.cleanup_complete
    assert backend.driver.closed


async def test_failed_termination_keeps_cleanup_incomplete(backend: SimpleNamespace) -> None:
    """驱动未关闭且强制终止也失败时，驱动保持开启并报告收尾未完成。"""

    def broken_terminate() -> None:
        raise OSError("SECRET_DRIVER")

    backend.connection.close.side_effect = OSError("SECRET")
    backend.driver.terminate = broken_terminate
    result = await command(backend).run(deadline=deadline())
    assert result.confirmed == [1, 2]
    assert not result.cleanup_complete and not backend.driver.closed


async def test_cancel_during_cleanup_propagates_and_reports_incomplete(backend: SimpleNamespace) -> None:
    """收尾进行中被再次取消：不吞取消、保守报告未完成，已确认版本保留。"""
    entered = asyncio.Event()

    async def hang() -> None:
        entered.set()
        await asyncio.Event().wait()

    backend.connection.close.side_effect = hang
    owner = command(backend)
    task = asyncio.create_task(owner.run(deadline=deadline()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.progress.confirmed == [1, 2]
    assert owner.progress.reason == "cleanup_incomplete" and not owner.progress.cleanup_complete
    assert backend.driver.closed


async def test_repeated_version_from_core_is_schema_mismatch(backend: SimpleNamespace) -> None:
    """核心重复返回已确认版本时按结构不匹配拒绝，不重复提交同一文件。"""
    backend.apply.side_effect = [1, 1]
    result = await command(backend).run(deadline=deadline())
    assert result.reason == "schema_mismatch" and result.confirmed == [1]


async def test_cancel_during_commit_retains_unknown_outcome(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()

    async def commit() -> None:
        entered.set()
        await asyncio.Event().wait()

    async def prepare(*args: Any, **kwargs: Any) -> None:
        backend.holder.transactions[-1].commit.side_effect = commit

    backend.prepare.side_effect = prepare
    owner = command(backend)
    task = asyncio.create_task(owner.run(deadline=deadline()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.progress.reason == "commit_unknown"
    assert owner.progress.uncertain_version == 1 and owner.progress.confirmed == []
    assert backend.apply.await_count == 1
    backend.holder.transactions[0].rollback.assert_awaited_once()
