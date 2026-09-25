"""会话 Store 的真实 PostgreSQL 行为、事务和资源边界。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.shared.exceptions import PersistenceUnavailableError

from .test_database_migrations import upgrade


@pytest.fixture
async def store(pg_engine):
    from app.infrastructure.session_store import PostgresSessionStore

    assert (await upgrade(pg_engine)).reason == "ok"
    return PostgresSessionStore(
        async_sessionmaker(pg_engine, expire_on_commit=False),
        operation_timeout_seconds=3,
        cleanup_timeout_seconds=0.5,
        is_available=lambda: True,
    )


async def create(store, sid="s1", user="u1", title="title"):
    return await store.create_session(session_id=sid, user_id=user, system_prompt="prompt", title=title)


async def test_crud_stats_and_message_window(store):
    created = await create(store)
    assert created["id"] == "s1" and created["user_id"] == "u1"
    assert created["message_count"] == created["total_tokens"] == 0
    assert await store.get_session("s1") == created
    assert await store.get_session("absent") is None
    await store.add_message("s1", "system", "hidden", token_count=100)
    first = await store.add_message("s1", "user", "first", token_count=2)
    second = await store.add_message("s1", "assistant", "second", reasoning_content="not history", token_count=3)
    third = await store.add_message("s1", "user", "third", token_count=4)
    assert 0 < first < second < third
    assert await store.get_messages("s1", limit=1) == [{"role": "user", "content": "third"}]
    assert await store.get_messages("s1", limit=1, offset=1) == [{"role": "assistant", "content": "second"}]
    assert await store.get_messages("s1", before_message_id=third) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    stats = await store.get_session_stats("s1")
    assert stats["message_count"] == 3 and stats["total_tokens"] == 9 and stats["last_message_at"]
    assert await store.get_session_stats("absent") == {"message_count": 0, "total_tokens": 0, "last_message_at": None}
    await store.delete_session("s1")
    assert await store.list_sessions("u1") == []
    assert len(await store.get_messages("s1")) == 3
    await store.hard_delete_session("s1")
    assert await store.get_session("s1") is None and await store.get_messages("s1") == []


async def test_enhanced_filters_counts_and_order(store, pg_engine):
    for sid, user, title in [("a", "u1", "Alpha"), ("b", "u1", "Beta"), ("c", "u1", "Gamma"), ("d", "u2", "Other")]:
        await create(store, sid, user, title)
    now = datetime.now(UTC)
    async with pg_engine.begin() as connection:
        await connection.execute(text("UPDATE public.sessions SET created_at=:now"), {"now": now})
        await connection.execute(
            text("UPDATE public.sessions SET status='archived',updated_at=:now WHERE id='b'"), {"now": now}
        )
        await connection.execute(text("UPDATE public.sessions SET status='deleted' WHERE id='c'"))
    assert [r["id"] for r in await store.list_sessions("u1")] == ["a"]
    rows, total = await store.list_sessions_v2("u1", status=None, sort_by="title", sort_order="asc", limit=1)
    assert total == 2 and [r["id"] for r in rows] == ["a"]
    rows, total = await store.list_sessions_v2(
        "u1", status="archived", keyword="ET", start_date=now - timedelta(seconds=1), end_date=now
    )
    assert total == 1 and rows[0]["id"] == "b"
    rows, total = await store.list_sessions_v2("u1", status="deleted")
    assert total == 1 and rows[0]["id"] == "c"
    _, total = await store.list_sessions_v2("u1", status="unknown")
    assert total == 3  # 保留既有未知 status 不追加筛选的行为。


async def test_hard_delete_failure_rolls_back_both_tables(store, pg_engine):
    await create(store)
    await store.add_message("s1", "user", "kept")

    def fail_session_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM public.sessions"):
            raise OSError("secret-sentinel")

    event.listen(pg_engine.sync_engine, "before_cursor_execute", fail_session_delete)
    try:
        with pytest.raises(PersistenceUnavailableError) as caught:
            await store.hard_delete_session("s1")
        assert "secret-sentinel" not in str(caught.value)
    finally:
        event.remove(pg_engine.sync_engine, "before_cursor_execute", fail_session_delete)
    assert await store.get_session("s1") is not None
    assert await store.get_messages("s1") == [{"role": "user", "content": "kept"}]


async def test_commit_response_loss_is_not_retried(store, pg_engine, monkeypatch):
    original = AsyncSession.commit
    calls = 0

    async def lose_response(session):
        nonlocal calls
        calls += 1
        await original(session)
        raise OSError("secret-sentinel")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", lose_response)
        with pytest.raises(PersistenceUnavailableError) as caught:
            await create(store)
        assert caught.value.commit_unknown and calls == 1
    async with pg_engine.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM public.sessions"))).scalar_one() == 1


async def test_real_query_timeout_releases_session(pg_engine):
    from app.infrastructure.session_store import PostgresSessionStore

    assert (await upgrade(pg_engine)).reason == "ok"

    class SlowSession(AsyncSession):
        async def execute(self, *args, **kwargs):
            await super().execute(text("SELECT pg_sleep(5)"))
            return await super().execute(*args, **kwargs)

    store = PostgresSessionStore(
        async_sessionmaker(pg_engine, class_=SlowSession, expire_on_commit=False),
        operation_timeout_seconds=0.3,
        cleanup_timeout_seconds=0.1,
        is_available=lambda: True,
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(PersistenceUnavailableError) as caught:
        await store.get_session("s1")
    assert caught.value.reason == "timeout"
    assert asyncio.get_running_loop().time() - started < 2
    async with pg_engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1


async def test_manager_cache_io_never_holds_database_connection(store, pg_engine):
    from app.application.session.session_manager import SessionManager

    class ObservedRedis:
        async def get(self, key):
            assert pg_engine.pool.checkedout() == 0

        async def set(self, key, value, ex):
            assert pg_engine.pool.checkedout() == 0

        async def delete(self, key):
            assert pg_engine.pool.checkedout() == 0

    manager = SessionManager(redis_client=ObservedRedis(), store=store)
    result = await manager.create_session("u1")
    assert (await manager.get_session(result["id"]))["id"] == result["id"]
    await manager.add_message(result["id"], "user", "hello", token_count=2)
    rows = await manager.list_sessions("u1")
    assert rows[0]["message_count"] == 1 and rows[0]["total_tokens"] == 2
    rows, total = await manager.list_sessions_v2("u1")
    assert total == 1 and rows[0]["message_count"] == 1
    await manager.hard_delete_session(result["id"])


async def test_equal_timestamps_use_id_order_and_exclusive_boundary(store, pg_engine):
    await create(store)
    ids = [await store.add_message("s1", "user", str(i)) for i in range(4)]
    async with pg_engine.begin() as connection:
        await connection.execute(
            text("UPDATE public.messages SET created_at=:stamp"), {"stamp": datetime(2026, 1, 1, tzinfo=UTC)}
        )
    assert await store.get_messages("s1", limit=2, before_message_id=ids[3]) == [
        {"role": "user", "content": "1"},
        {"role": "user", "content": "2"},
    ]
    assert await store.get_messages("s1", limit=1, offset=1, before_message_id=ids[3]) == [
        {"role": "user", "content": "1"},
    ]
