"""app/application/session/session_manager.py SessionManager 单元测试

用真实 SessionModel/MessageModel 实例作行 + 手写 _FakeRedis/_FakeDB。
_FakeDB.execute 按 SQLAlchemy 语句类型分发：
  - Insert/Update/Delete 按 stmt.table.name 区分 sessions / messages
  - Select 按 column_descriptions[0]["entity"] 区分实体行，纯函数列按列数区分
    （3 列 = _get_session_stats 聚合，1 列 = list_sessions_v2 的 func.count）
"""

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import Delete, Insert, Select, Update

from app.application.session.session_manager import SessionManager
from app.infrastructure.models.database import MessageModel, SessionModel


class _FakeRedis:
    """async redis：set(key, value, ttl) / get / delete"""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.deleted: list[str] = []

    async def set(self, key, value, ttl=None):
        self.data[key] = value
        self.ttls[key] = ttl

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)
        self.deleted.append(key)


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows=(), inserted_primary_key=()):
        self._rows = list(rows)
        self.inserted_primary_key = inserted_primary_key

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return _FakeScalars(self._rows)

    def one(self):
        return self._rows[0]

    def scalar(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt):
        self.db.all_statements.append(stmt)
        return self.db.dispatch(stmt)

    async def commit(self):
        self.db.commits += 1


class _FakeDB:
    """callable 作 db_session_factory：async with factory() as db"""

    def __init__(self):
        self.sessions: list[SessionModel] = []
        self.messages: list[MessageModel] = []
        self.stats_row = None  # 3 列聚合行（message_count/total_tokens/last_message_at）
        self.count_value: int = 0  # list_sessions_v2 总数
        self.commits = 0
        self.all_statements: list = []
        self._next_message_id = 1

    def __call__(self):
        return _FakeSession(self)

    def dispatch(self, stmt):
        if isinstance(stmt, Insert):
            if stmt.table.name == "messages":
                new_id = self._next_message_id
                self._next_message_id += 1
                return _FakeResult(inserted_primary_key=(new_id,))
            return _FakeResult()
        if isinstance(stmt, (Update, Delete)):
            return _FakeResult()
        if isinstance(stmt, Select):
            descs = stmt.column_descriptions
            expr = descs[0]["expr"] if descs else None
            # 实体行 select：expr 即映射类（SessionModel/MessageModel）
            if isinstance(expr, type):
                if expr is SessionModel:
                    return _FakeResult(self.sessions)
                if expr is MessageModel:
                    return _FakeResult(self.messages)
            # 纯函数列：3 列 = stats 聚合，1 列 = count
            if len(descs) == 3:
                return _FakeResult([self.stats_row])
            return _FakeResult([self.count_value])
        raise TypeError(f"unhandled statement: {stmt}")


def _make_manager(redis=None, db=None) -> tuple[SessionManager, _FakeDB]:
    fake_db = db or _FakeDB()
    return SessionManager(redis_client=redis, db_session_factory=fake_db), fake_db


def _session_row(session_id="s1", user_id="u1", **kw) -> SessionModel:
    """构造显式字段的 SessionModel 行（insert-time 默认不会在普通构造时触发）"""
    defaults = {
        "title": "t",
        "system_prompt": "p",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "status": "active",
    }
    defaults.update(kw)
    return SessionModel(id=session_id, user_id=user_id, **defaults)


def _message_row(message_id, session_id="s1", role="user", content="hi") -> MessageModel:
    return MessageModel(
        id=message_id,
        session_id=session_id,
        role=role,
        content=content,
        token_count=1,
        created_at=datetime.now(UTC),
    )


# ===== 构造 =====


def test_init_warns_when_redis_none(caplog):
    fake_db = _FakeDB()
    with caplog.at_level(logging.WARNING):
        SessionManager(redis_client=None, db_session_factory=fake_db)
    assert "Redis 不可用" in caplog.text


# ===== create_session =====


@pytest.mark.asyncio
async def test_create_session_persists_and_caches():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    result = await sm.create_session("u1", system_prompt="p", title="t")

    assert result["user_id"] == "u1"
    assert result["system_prompt"] == "p"
    assert result["message_count"] == 0
    assert result["total_tokens"] == 0
    assert "id" in result
    assert fake_db.commits == 1

    key = f"session:{result['id']}"
    assert json.loads(fake_redis.data[key])["user_id"] == "u1"
    assert fake_redis.ttls[key] == sm.session_ttl


@pytest.mark.asyncio
async def test_create_session_applies_defaults():
    fake_redis = _FakeRedis()
    sm, _ = _make_manager(redis=fake_redis)

    result = await sm.create_session("u1")

    assert result["system_prompt"] == "你是一个友好的AI助手"


# ===== get_session =====


@pytest.mark.asyncio
async def test_get_session_returns_redis_cache():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    fake_redis.data["session:s1"] = json.dumps({"id": "s1", "user_id": "u1"})
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result = await sm.get_session("s1")

    assert result == {"id": "s1", "user_id": "u1"}
    assert fake_db.all_statements == []  # 缓存命中不查库


@pytest.mark.asyncio
async def test_get_session_db_fallback_and_writeback():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    fake_db.sessions = [_session_row()]
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result = await sm.get_session("s1")

    assert result["id"] == "s1"
    assert result["user_id"] == "u1"
    assert result["system_prompt"] == "p"
    assert "session:s1" in fake_redis.data
    assert fake_redis.ttls["session:s1"] == sm.session_ttl


@pytest.mark.asyncio
async def test_get_session_returns_none_when_missing():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()  # sessions 为空
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    assert await sm.get_session("s1") is None
    assert "session:s1" not in fake_redis.data


# ===== get_messages =====


@pytest.mark.asyncio
async def test_get_messages_maps_role_content():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    fake_db.messages = [
        _message_row(1, role="user", content="hi"),
        _message_row(2, role="assistant", content="yo"),
    ]
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result = await sm.get_messages("s1", limit=10)

    assert result == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ]


# ===== add_message =====


@pytest.mark.asyncio
async def test_add_message_returns_inserted_id():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    first = await sm.add_message("s1", "user", "hello", token_count=3)
    second = await sm.add_message("s1", "assistant", "world")

    assert first == 1
    assert second == 2
    assert fake_db.commits == 2


# ===== delete_session / hard_delete_session =====


@pytest.mark.asyncio
async def test_delete_session_soft_delete_and_redis():
    fake_redis = _FakeRedis()
    fake_redis.data["session:s1"] = "{}"
    sm, fake_db = _make_manager(redis=fake_redis)

    await sm.delete_session("s1")

    assert "session:s1" not in fake_redis.data
    assert fake_db.commits == 1
    stmt = fake_db.all_statements[0]
    assert isinstance(stmt, Update)
    assert stmt.table.name == "sessions"


@pytest.mark.asyncio
async def test_hard_delete_session_messages_then_session():
    fake_redis = _FakeRedis()
    fake_redis.data["session:s1"] = "{}"
    sm, fake_db = _make_manager(redis=fake_redis)

    await sm.hard_delete_session("s1")

    stmts = fake_db.all_statements
    assert isinstance(stmts[0], Delete) and stmts[0].table.name == "messages"
    assert isinstance(stmts[1], Delete) and stmts[1].table.name == "sessions"
    assert fake_db.commits == 1
    assert "session:s1" not in fake_redis.data


# ===== list_sessions =====


@pytest.mark.asyncio
async def test_list_sessions_clamps_limit_and_offset():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    await sm.list_sessions("u1", limit=500, include_stats=False)
    assert fake_db.all_statements[-1]._limit == 100  # 钳制上限

    fake_db.all_statements.clear()
    fake_redis.data.clear()  # 清掉第一页缓存，避免二次调用命中缓存不查库
    await sm.list_sessions("u1", limit=20, offset=-5, include_stats=False)
    assert fake_db.all_statements[-1]._offset == 0  # 负数归零


@pytest.mark.asyncio
async def test_list_sessions_caches_only_first_page():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    await sm.list_sessions("u1", limit=10, offset=0, include_stats=False)
    assert "user_sessions:u1:page:0" in fake_redis.data
    assert fake_redis.ttls["user_sessions:u1:page:0"] == 30

    fake_redis.data.clear()
    fake_db.all_statements.clear()
    await sm.list_sessions("u1", limit=10, offset=20, include_stats=False)
    assert "user_sessions:u1:page:2" not in fake_redis.data


@pytest.mark.asyncio
async def test_list_sessions_builds_items_and_stats():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    now = datetime.now(UTC)
    fake_db.sessions = [_session_row(updated_at=None, created_at=now)]
    fake_db.stats_row = SimpleNamespace(message_count=3, total_tokens=10, last_message_at=None)
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result = await sm.list_sessions("u1")

    item = result[0]
    assert item["id"] == "s1"
    assert item["created_at"] == now.isoformat()
    assert item["updated_at"] is None
    assert item["status"] == "active"
    assert item["message_count"] == 3
    assert item["total_tokens"] == 10
    assert item["last_message_at"] is None
    assert fake_redis.ttls["session_stats:s1"] == 60


@pytest.mark.asyncio
async def test_list_sessions_without_stats_no_message_count():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    fake_db.sessions = [_session_row()]
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result = await sm.list_sessions("u1", include_stats=False)

    assert "message_count" not in result[0]


# ===== _get_session_stats =====


@pytest.mark.asyncio
async def test_get_session_stats_redis_cache_first():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    fake_redis.data["session_stats:s1"] = json.dumps(
        {"message_count": 9, "total_tokens": 99, "last_message_at": None}
    )
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result = await sm._get_session_stats("s1", _FakeSession(fake_db))

    assert result == {"message_count": 9, "total_tokens": 99, "last_message_at": None}
    assert fake_db.all_statements == []  # 缓存命中不查库


@pytest.mark.asyncio
async def test_get_session_stats_db_aggregate_and_cache():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    fake_db.stats_row = SimpleNamespace(message_count=5, total_tokens=12, last_message_at=None)
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result = await sm._get_session_stats("s1", _FakeSession(fake_db))

    assert result == {"message_count": 5, "total_tokens": 12, "last_message_at": None}
    assert "session_stats:s1" in fake_redis.data
    assert fake_redis.ttls["session_stats:s1"] == 60
    stmt = fake_db.all_statements[0]
    assert isinstance(stmt, Select)
    assert len(stmt.column_descriptions) == 3


# ===== list_sessions_v2 =====


@pytest.mark.asyncio
async def test_list_sessions_v2_returns_list_and_total():
    fake_redis = _FakeRedis()
    fake_db = _FakeDB()
    fake_db.sessions = [_session_row(), _session_row(session_id="s2", title="t2")]
    fake_db.count_value = 42
    fake_db.stats_row = SimpleNamespace(message_count=0, total_tokens=0, last_message_at=None)
    sm = SessionManager(redis_client=fake_redis, db_session_factory=fake_db)

    result, total = await sm.list_sessions_v2("u1")

    assert len(result) == 2
    assert total == 42
    assert isinstance(fake_db.all_statements[0], Select)  # 先 count 再分页


@pytest.mark.asyncio
async def test_list_sessions_v2_status_archived_conditions():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    await sm.list_sessions_v2("u1", status="archived", include_stats=False)

    count_stmt = fake_db.all_statements[0]
    assert len(count_stmt._where_criteria) == 2  # user_id + status


@pytest.mark.asyncio
async def test_list_sessions_v2_status_none_excludes_deleted():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    await sm.list_sessions_v2("u1", status=None, include_stats=False)

    count_stmt = fake_db.all_statements[0]
    assert len(count_stmt._where_criteria) == 2  # user_id + status != deleted


@pytest.mark.asyncio
async def test_list_sessions_v2_keyword_and_dates():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    await sm.list_sessions_v2(
        "u1",
        status=None,
        keyword="ab",
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        include_stats=False,
    )

    count_stmt = fake_db.all_statements[0]
    # user + (status != deleted) + ilike + start + end = 5 个条件
    assert len(count_stmt._where_criteria) == 5


@pytest.mark.asyncio
async def test_list_sessions_v2_sort_by_fallback_and_asc():
    fake_redis = _FakeRedis()
    sm, fake_db = _make_manager(redis=fake_redis)

    # 未知 sort_by 回退 updated_at，asc 走 nullsfirst，均不应抛错
    await sm.list_sessions_v2("u1", sort_by="bogus", sort_order="asc", include_stats=False)

    assert isinstance(fake_db.all_statements[0], Select)
