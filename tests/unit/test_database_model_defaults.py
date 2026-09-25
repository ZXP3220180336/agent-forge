"""真实 PostgreSQL 编译器/客户端默认处理器单测；不证明数据库写入或 DDL。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from sqlalchemy import insert, update
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

from app.infrastructure.models.database import messages, session


def execute_defaults(statement) -> dict:
    """执行 SQLAlchemy 的参数预处理，保留发送给驱动前的 Python 值。"""
    dialect = PGDialect_asyncpg()
    compiled = statement.compile(dialect=dialect)
    context = dialect.execution_ctx_cls.__new__(dialect.execution_ctx_cls)
    context.compiled = compiled
    context.compiled_parameters = [compiled.construct_params()]
    context._process_execute_defaults()
    return context.compiled_parameters[0]


@pytest.mark.parametrize(
    ("module", "model", "values"),
    [
        (session, session.SessionModel, {"id": "session", "user_id": "user"}),
        (messages, messages.MessageModel, {"id": 1, "role": "user", "content": "hello"}),
    ],
)
def test_created_at_uses_each_execution_time(monkeypatch, module, model, values) -> None:
    """两次执行各自取得当前 UTC 时刻，不能复用 import 时的时间。"""
    first = datetime(2026, 9, 24, 1, tzinfo=UTC)
    second = datetime(2026, 9, 24, 2, tzinfo=UTC)
    clock = Mock(side_effect=[first, second])
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=clock))
    statement = insert(model).values(**values)

    assert execute_defaults(statement)["created_at"] == first
    assert execute_defaults(statement)["created_at"] == second
    assert clock.call_args_list == [call(UTC), call(UTC)]


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (session.SessionModel, {"id": "session", "user_id": "user"}),
        (messages.MessageModel, {"id": 1, "role": "user", "content": "hello"}),
    ],
)
def test_meta_default_is_independent_for_each_execution(model, values) -> None:
    """改变一条执行参数的 JSON 对象不能污染下一条默认值。"""
    statement = insert(model).values(**values)
    first = execute_defaults(statement)["meta"]
    second = execute_defaults(statement)["meta"]

    assert first is not second
    first["local"] = True
    assert second == {}


def test_updated_at_uses_each_update_time(monkeypatch) -> None:
    """更新同一会话两次时，每次执行分别取时间。"""
    first = datetime(2026, 9, 24, 1, tzinfo=UTC)
    second = datetime(2026, 9, 24, 2, tzinfo=UTC)
    monkeypatch.setattr(session, "datetime", SimpleNamespace(now=Mock(side_effect=[first, second])))
    statement = update(session.SessionModel).where(session.SessionModel.id == "session").values(title="new")

    assert execute_defaults(statement)["updated_at"] == first
    assert execute_defaults(statement)["updated_at"] == second


def test_client_defaults_do_not_introduce_server_or_updated_at_insert_defaults() -> None:
    """D3 仅修客户端求值时机，不增加 SQL 默认或更新时间插入默认。"""
    assert "updated_at" not in execute_defaults(insert(session.SessionModel).values(id="s", user_id="u"))
    for model in (session.SessionModel, messages.MessageModel):
        for column in model.__table__.columns:
            assert column.server_default is None
            assert column.server_onupdate is None
