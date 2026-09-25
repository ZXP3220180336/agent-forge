"""SessionManager 只消费持久化端口；保护缓存、参数与失败边界。"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from unittest.mock import Mock, call
from uuid import UUID

import pytest

from app.application.session.session_manager import SessionManager
from app.domain.ports.session_store import SessionStorePort
from app.shared.exceptions import PersistenceUnavailableError


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}
        self.calls = []

    async def get(self, key):
        self.calls.append(("get", key))
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.calls.append(("set", key))
        self.data[key] = value
        self.ttls[key] = ttl

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.data.pop(key, None)


def _store():
    store = Mock(spec=SessionStorePort)
    store.get_session.return_value = {"id": "s1", "user_id": "u1"}
    store.get_messages.return_value = [{"role": "user", "content": "hi"}]
    store.add_message.return_value = 42
    store.list_sessions.return_value = [{"id": "s1", "title": "t", "updated_at": None}]
    store.list_sessions_v2.return_value = ([{"id": "s1", "title": "t"}], 42)
    store.get_session_stats.return_value = {"message_count": 3, "total_tokens": 10, "last_message_at": None}
    return store


@pytest.mark.parametrize(
    "operation,args",
    [
        ("create_session", ("u1",)),
        ("get_session", ("s1",)),
        ("get_messages", ("s1",)),
        ("add_message", ("s1", "user", "hi")),
        ("delete_session", ("s1",)),
        ("hard_delete_session", ("s1",)),
        ("list_sessions", ("u1",)),
        ("list_sessions_v2", ("u1",)),
    ],
)
@pytest.mark.parametrize("missing", [True, False])
async def test_unavailable_rejects_before_any_cache_access(operation, args, missing):
    cache = _FakeRedis()
    cache.data.update({"session:s1": "{}", "user_sessions:u1:page:0": "[]"})
    store = None if missing else _store()
    if store is not None:
        store.ensure_available.side_effect = PersistenceUnavailableError()
    manager = SessionManager(cache, store)
    with pytest.raises(PersistenceUnavailableError) as exc:
        await getattr(manager, operation)(*args)
    assert exc.value.code == "PERSISTENCE_UNAVAILABLE"
    assert cache.calls == []
    if store is not None:
        assert store.mock_calls == [call.ensure_available()]


def test_init_warns_when_redis_none(caplog):
    with caplog.at_level(logging.WARNING):
        SessionManager(None, _store())
    assert "Redis 不可用" in caplog.text


@pytest.mark.parametrize("prompt,title", [(None, None), ("", ""), ("p", "t")])
async def test_create_uses_defaults_uuid_and_caches_only_after_store_returns(prompt, title):
    cache, store = _FakeRedis(), _store()

    async def create(**values):
        assert cache.calls == []
        return {
            "id": values["session_id"],
            "user_id": values["user_id"],
            "system_prompt": values["system_prompt"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "message_count": 0,
            "total_tokens": 0,
        }

    store.create_session.side_effect = create
    manager = SessionManager(cache, store)
    result = await manager.create_session("u1", prompt, title)
    values = store.create_session.call_args.kwargs
    assert str(UUID(values["session_id"])) == result["id"]
    assert values["title"] == (title or "新对话")
    assert values["system_prompt"] == (prompt or "你是一个友好的AI助手")
    key = "session:" + result["id"]
    assert json.loads(cache.data[key]) == result
    assert cache.ttls[key] == 604800


async def test_create_failure_is_not_retried_or_cached():
    cache, store = _FakeRedis(), _store()
    error = PersistenceUnavailableError(commit_unknown=True)
    store.create_session.side_effect = error
    with pytest.raises(PersistenceUnavailableError) as exc:
        await SessionManager(cache, store).create_session("u1")
    assert exc.value is error
    store.create_session.assert_awaited_once()
    assert cache.calls == []


async def test_create_cancellation_propagates_without_cache_or_retry():
    cache, store = _FakeRedis(), _store()
    store.create_session.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await SessionManager(cache, store).create_session("u1")
    store.create_session.assert_awaited_once()
    assert cache.calls == []


async def test_get_session_cache_hit_still_checks_availability():
    cache, store = _FakeRedis(), _store()
    cache.data["session:s1"] = '{"id":"s1"}'
    assert await SessionManager(cache, store).get_session("s1") == {"id": "s1"}
    assert store.ensure_available.call_count == 2
    store.get_session.assert_not_awaited()


@pytest.mark.parametrize(
    "operation,args,key,cached",
    [
        ("get_session", ("s1",), "session:s1", '{"id":"s1"}'),
        ("list_sessions", ("u1",), "user_sessions:u1:page:0", '[{"id":"s1"}]'),
        ("_get_session_stats", ("s1",), "session_stats:s1", '{"message_count":1}'),
    ],
)
async def test_cache_hit_rechecks_availability_after_redis_wait(operation, args, key, cached):
    """Redis 等待期间持久化变为不可用，不能再把旧缓存当作成功返回。"""
    store = _store()

    class StateChangingRedis(_FakeRedis):
        async def get(self, requested_key):
            assert requested_key == key
            store.ensure_available.side_effect = PersistenceUnavailableError()
            return cached

    manager = SessionManager(StateChangingRedis(), store)
    with pytest.raises(PersistenceUnavailableError):
        await getattr(manager, operation)(*args)
    store.get_session.assert_not_awaited()
    store.list_sessions.assert_not_awaited()
    store.get_session_stats.assert_not_awaited()


@pytest.mark.parametrize("found", [True, False])
async def test_get_session_miss_caches_only_existing_row(found):
    cache, store = _FakeRedis(), _store()
    expected = {"id": "s1"} if found else None
    store.get_session.return_value = expected
    assert await SessionManager(cache, store).get_session("s1") == expected
    assert ("session:s1" in cache.data) is found
    if found:
        assert cache.ttls["session:s1"] == 604800


async def test_no_redis_reads_store_and_forwards_message_window():
    store = _store()
    manager = SessionManager(None, store)
    assert await manager.get_session("s1") == {"id": "s1", "user_id": "u1"}
    result = await manager.get_messages("s1", limit=10, offset=3, before_message_id=42)
    assert result == [{"role": "user", "content": "hi"}]
    store.get_messages.assert_awaited_once_with("s1", limit=10, offset=3, before_message_id=42)


async def test_add_message_forwards_payload_and_returns_committed_id():
    store = _store()
    assert await SessionManager(None, store).add_message("s1", "assistant", "hi", "reason", 8) == 42
    store.add_message.assert_awaited_once_with("s1", "assistant", "hi", reasoning_content="reason", token_count=8)


@pytest.mark.parametrize("operation", ["delete_session", "hard_delete_session"])
async def test_delete_keeps_existing_cache_before_store_order(operation):
    cache, store = _FakeRedis(), _store()
    cache.data["session:s1"] = "{}"

    async def deleted(session_id):
        assert cache.calls == [("delete", "session:s1")]

    getattr(store, operation).side_effect = deleted
    await getattr(SessionManager(cache, store), operation)("s1")
    getattr(store, operation).assert_awaited_once_with("s1")
    assert "session:s1" not in cache.data


@pytest.mark.parametrize("limit,offset,expected", [(500, -5, (100, 0)), (0, 20, (1, 20))])
async def test_list_clamps_pagination_and_caches_first_page_only(limit, offset, expected):
    cache, store = _FakeRedis(), _store()
    result = await SessionManager(cache, store).list_sessions("u1", limit, offset, include_stats=False)
    store.list_sessions.assert_awaited_once_with("u1", limit=expected[0], offset=expected[1])
    assert result == store.list_sessions.return_value
    assert bool(cache.data) is (expected[1] == 0)
    if expected[1] == 0:
        assert cache.ttls["user_sessions:u1:page:0"] == 30
    store.get_session_stats.assert_not_awaited()


async def test_list_cache_hit_skips_store_queries():
    cache, store = _FakeRedis(), _store()
    cache.data["user_sessions:u1:page:0"] = '[{"id":"cached"}]'
    assert await SessionManager(cache, store).list_sessions("u1") == [{"id": "cached"}]
    assert store.ensure_available.call_count == 2
    store.list_sessions.assert_not_awaited()
    store.get_session_stats.assert_not_awaited()


@pytest.mark.parametrize("cached_stats", [True, False])
async def test_list_merges_stats_and_preserves_stats_cache(cached_stats):
    cache, store = _FakeRedis(), _store()
    stats = {"message_count": 3, "total_tokens": 10, "last_message_at": None}
    if cached_stats:
        cache.data["session_stats:s1"] = json.dumps(stats)
    result = await SessionManager(cache, store).list_sessions("u1")
    assert result == [{"id": "s1", "title": "t", "updated_at": None, **stats}]
    if cached_stats:
        store.get_session_stats.assert_not_awaited()
    else:
        store.get_session_stats.assert_awaited_once_with("s1")
        assert cache.ttls["session_stats:s1"] == 60


@pytest.mark.parametrize("include_stats", [True, False])
async def test_list_v2_forwards_filters_and_keeps_total(include_stats):
    cache, store = _FakeRedis(), _store()
    start, end = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC)
    result, total = await SessionManager(cache, store).list_sessions_v2(
        "u1",
        limit=200,
        offset=-1,
        status=None,
        keyword="ab",
        start_date=start,
        end_date=end,
        sort_by="bogus",
        sort_order="asc",
        include_stats=include_stats,
    )
    assert total == 42
    assert ("message_count" in result[0]) is include_stats
    store.list_sessions_v2.assert_awaited_once_with(
        "u1",
        limit=100,
        offset=0,
        status=None,
        keyword="ab",
        start_date=start,
        end_date=end,
        sort_by="bogus",
        sort_order="asc",
    )
    assert not any(key.startswith("user_sessions:") for key in cache.data)
