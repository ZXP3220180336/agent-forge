"""迁移准备必须接上严格门禁，首次 SQL 必须保持既有数据类型。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.infrastructure import database_migrations as migrations


def test_production_schema_preparer_is_connected() -> None:
    assert callable(migrations.SCHEMA_PREPARER)


def test_initial_migration_is_one_flat_utf8_snapshot() -> None:
    snapshot = migrations.load_migrations(Path(__file__).resolve().parents[2] / "migrations")
    assert snapshot[0].name == "0001_sessions_and_messages.sql"
    sql = snapshot[0].sql.lower()
    assert "public.sessions" in sql and "public.messages" in sql
    assert "bigserial" in sql and "jsonb" not in sql
    assert "if not exists" not in sql


@pytest.fixture
def prepared(monkeypatch):
    """所有 SQL 由记录连接接收；只替换 catalog 边界，观察事务前置条件与副作用。"""
    connection = SimpleNamespace(
        in_transaction=lambda: True,
        sync_connection=SimpleNamespace(get_execution_options=dict),
        execute=AsyncMock(),
    )
    monkeypatch.setattr(
        migrations.schema, "managed_tables", AsyncMock(return_value={"schema_versions", "sessions", "messages"})
    )
    monkeypatch.setattr(migrations.schema, "validate_version_table", AsyncMock())
    monkeypatch.setattr(migrations.schema, "validate_session_tables", AsyncMock())
    monkeypatch.setattr(migrations, "_read_history", AsyncMock(return_value=()))
    snapshot = migrations.load_migrations(Path(__file__).resolve().parents[2] / "migrations")
    return connection, snapshot


async def test_baseline_does_not_register_when_catalog_rejects(prepared, monkeypatch):
    connection, snapshot = prepared
    monkeypatch.setattr(
        migrations.schema,
        "validate_session_tables",
        AsyncMock(side_effect=migrations.schema.SchemaError("schema_mismatch")),
    )
    with pytest.raises(migrations.MigrationError, match="schema_mismatch"):
        await migrations.prepare_schema(
            connection, snapshot, baseline_existing=True, deadline=asyncio.get_running_loop().time() + 1
        )
    assert all("INSERT" not in str(call.args[0]) for call in connection.execute.call_args_list)


async def test_nontransactional_preparation_never_queries_catalog(prepared):
    connection, snapshot = prepared
    connection.in_transaction = lambda: False
    with pytest.raises(migrations.MigrationError, match="transaction_required"):
        await migrations.prepare_schema(connection, snapshot, deadline=asyncio.get_running_loop().time() + 1)
    migrations.schema.managed_tables.assert_not_awaited()
    connection.execute.assert_not_awaited()


async def test_cancelled_baseline_never_registers(prepared, monkeypatch):
    connection, snapshot = prepared
    monkeypatch.setattr(migrations.schema, "validate_session_tables", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await migrations.prepare_schema(
            connection, snapshot, baseline_existing=True, deadline=asyncio.get_running_loop().time() + 1
        )
    assert all("INSERT" not in str(call.args[0]) for call in connection.execute.call_args_list)


async def test_expired_preparation_does_not_create_version_table(prepared):
    connection, snapshot = prepared
    with pytest.raises(migrations.MigrationError, match="timeout"):
        await migrations.prepare_schema(connection, snapshot, deadline=asyncio.get_running_loop().time() - 1)
    connection.execute.assert_not_awaited()
