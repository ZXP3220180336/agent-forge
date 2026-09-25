"""Store 生命周期单测：事务归属、故障脱敏与取消。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from app.infrastructure.session_store import PostgresSessionStore
from app.shared.exceptions import PersistenceUnavailableError


def make_store(db=None, **kwargs):
    db = db or SimpleNamespace(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(), close=AsyncMock())
    store = PostgresSessionStore(
        lambda: db, operation_timeout_seconds=0.05, cleanup_timeout_seconds=0.02, is_available=lambda: True, **kwargs
    )
    return store, db


@pytest.mark.asyncio
async def test_hard_delete_one_commit_and_close():
    store, db = make_store()
    await store.hard_delete_session("s")
    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()
    db.close.assert_awaited_once()
    assert "messages" in str(db.execute.call_args_list[0].args[0])
    assert "sessions" in str(db.execute.call_args_list[1].args[0])


@pytest.mark.asyncio
async def test_second_delete_failure_rolls_back_and_redacts():
    store, db = make_store()
    db.execute.side_effect = [None, OperationalError("secret SQL", {}, SimpleNamespace(sqlstate="08006"))]
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.hard_delete_session("s")
    assert "secret" not in str(caught.value)
    assert not caught.value.commit_unknown
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
    db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_failure_unknown_no_retry():
    store, db = make_store()
    db.commit.side_effect = OperationalError("secret", {}, SimpleNamespace(sqlstate="08006"))
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.delete_session("s")
    assert caught.value.commit_unknown
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_programming_error_not_translated():
    store, db = make_store()
    db.execute.side_effect = ValueError("bug")
    with pytest.raises(ValueError, match="bug"):
        await store.delete_session("s")
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_rolls_back():
    store, db = make_store()
    entered = asyncio.Event()

    async def execute(*args):
        entered.set()
        await asyncio.Event().wait()

    db.execute.side_effect = execute
    task = asyncio.create_task(store.delete_session("s"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    db.rollback.assert_awaited_once()
    db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_does_not_start_commit():
    store, db = make_store()

    async def execute(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    db.execute.side_effect = execute
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.delete_session("s")
    assert caught.value.reason == "timeout"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_cleanup_failure_retains_fact():
    store, db = make_store()
    db.close.side_effect = OperationalError("secret", {}, SimpleNamespace(sqlstate="08006"))
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.delete_session("s")
    assert caught.value.committed
    assert not caught.value.commit_unknown
    assert caught.value.reason == "cleanup_incomplete"


def test_unavailable_checks_before_factory():
    store = PostgresSessionStore(
        None, operation_timeout_seconds=1, cleanup_timeout_seconds=1, is_available=lambda: False
    )
    with pytest.raises(PersistenceUnavailableError):
        store.ensure_available()


@pytest.mark.asyncio
async def test_primary_failure_survives_slow_cleanup():
    store, db = make_store()
    db.execute.side_effect = ValueError("primary bug")

    async def close():
        await asyncio.Event().wait()

    db.close.side_effect = close
    with pytest.raises(ValueError, match="primary bug"):
        await store.delete_session("s")


@pytest.mark.asyncio
async def test_uncooperative_worker_is_retained_and_blocks_admission():
    store, db = make_store()
    release = asyncio.Event()

    async def execute(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    db.execute.side_effect = execute
    with pytest.raises(PersistenceUnavailableError):
        await asyncio.wait_for(store.delete_session("s"), 0.5)
    assert store.pending_count == 1
    with pytest.raises(PersistenceUnavailableError):
        store.ensure_available()
    db.close.assert_not_awaited()
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert store.pending_count == 0
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_cancel_preserves_unknown():
    store, db = make_store()
    entered = asyncio.Event()

    async def commit():
        entered.set()
        await asyncio.Event().wait()

    db.commit.side_effect = commit
    task = asyncio.create_task(store.delete_session("s"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.commit_unknown
    assert not caught.value.committed


@pytest.mark.asyncio
async def test_late_commit_response_retains_confirmed_fact():
    store, db = make_store()

    async def commit():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    db.commit.side_effect = commit
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.delete_session("s")
    assert caught.value.committed
    assert not caught.value.commit_unknown
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_primary_key_invalid_rolls_back():
    store, db = make_store()
    db.execute.return_value = SimpleNamespace(inserted_primary_key=(True,))
    with pytest.raises(RuntimeError, match="主键"):
        await store.add_message("s", "user", "hello")
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_cleanup_bug_is_not_unavailable():
    store, db = make_store()
    db.close.side_effect = ValueError("cleanup bug")
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    with pytest.raises(ValueError, match="cleanup bug"):
        await store.get_session("s")


@pytest.mark.asyncio
async def test_commit_blocks_loop_beyond_deadline_retains_fact():
    import time

    store, db = make_store()

    async def commit():
        time.sleep(0.06)  # noqa: ASYNC251 -- 故意复现事件循环阻塞后的迟到提交。

    db.commit.side_effect = commit
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.delete_session("s")
    assert caught.value.committed
    assert not caught.value.commit_unknown


@pytest.mark.asyncio
async def test_cleanup_swallows_cancel_cannot_report_success():
    store, db = make_store()

    async def close():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    db.close.side_effect = close
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.delete_session("s")
    assert caught.value.committed
    assert caught.value.reason == "cleanup_incomplete"


@pytest.mark.asyncio
async def test_unknown_dbapi_error_is_not_recoverable_unavailable():
    from sqlalchemy.exc import IntegrityError

    store, db = make_store()
    db.execute.side_effect = IntegrityError("constraint bug", {}, Exception())
    with pytest.raises(IntegrityError):
        await store.delete_session("s")
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_cancel_during_cleanup_keeps_single_owner():
    store, db = make_store()
    executing = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    async def execute(*args):
        executing.set()
        await asyncio.Event().wait()

    async def close():
        closing.set()
        await release.wait()

    db.execute.side_effect = execute
    db.close.side_effect = close
    task = asyncio.create_task(store.delete_session("s"))
    await executing.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.pending_count == 1
    assert db.close.await_count == 1
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert store.pending_count == 0


@pytest.mark.asyncio
async def test_late_cleanup_bug_is_not_recoverable_timeout():
    store, db = make_store()

    async def close():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise ValueError("late cleanup bug") from None

    db.close.side_effect = close
    with pytest.raises(ValueError, match="late cleanup bug"):
        await store.delete_session("s")


@pytest.mark.asyncio
async def test_unknown_cleanup_dbapi_preserves_type_and_commit_fact():
    from sqlalchemy.exc import ProgrammingError

    store, db = make_store()
    db.close.side_effect = ProgrammingError("cleanup programming bug", {}, Exception())
    with pytest.raises(ProgrammingError) as caught:
        await store.delete_session("s")
    assert caught.value.committed
    assert not caught.value.commit_unknown


@pytest.mark.asyncio
async def test_unknown_cleanup_value_error_preserves_commit_fact():
    store, db = make_store()
    db.close.side_effect = ValueError("cleanup bug")
    with pytest.raises(ValueError) as caught:
        await store.delete_session("s")
    assert caught.value.committed
    assert not caught.value.commit_unknown
