"""真实 PostgreSQL 迁移验收；仅允许明确授权的专用测试库。"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SAWarning

from app.infrastructure import database_migrations as module

ROOT = Path(__file__).resolve().parents[2]


def deadline() -> float:
    return asyncio.get_running_loop().time() + 10


def snapshot():
    return module.load_migrations(ROOT / "migrations")


async def upgrade(engine, *, baseline=False, migrations=None):
    """测试沿生产命令 Owner 执行，复用相同 preparer 与事务核心。"""
    command = module.MigrationCommand(
        engine,
        migrations or snapshot(),
        timeouts=module.MigrationTimeouts(file_timeout_seconds=10, total_timeout_seconds=20, cleanup_timeout_seconds=2),
        prepare_schema=module.SCHEMA_PREPARER,
        baseline_existing=baseline,
    )
    return await command.run(deadline=asyncio.get_running_loop().time() + 20)


async def test_clean_upgrade_and_repeat_are_ready(pg_engine):
    first = await upgrade(pg_engine)
    assert first.reason == "ok" and first.confirmed == [1] and first.cleanup_complete
    second = await upgrade(pg_engine)
    assert second.reason == "ok" and second.confirmed == []
    async with pg_engine.connect() as connection:
        assert await module.check_schema(connection, snapshot(), deadline=deadline()) == 1


async def test_failed_batch_rolls_back_version_table_and_business_ddl(pg_engine, tmp_path):
    sql = snapshot()[0].sql + "\nSELECT 1/0;\n"
    (tmp_path / "0001_sessions_and_messages.sql").write_text(sql, encoding="utf-8", newline="\n")
    result = await upgrade(pg_engine, migrations=module.load_migrations(tmp_path))
    assert result.reason == "migration_database_error" and result.confirmed == []
    async with pg_engine.connect() as connection:
        assert (await connection.execute(text("SELECT to_regclass('public.schema_versions')"))).scalar() is None
        assert (await connection.execute(text("SELECT to_regclass('public.sessions')"))).scalar() is None


async def test_existing_data_requires_explicit_baseline_and_is_preserved(pg_engine):
    async with pg_engine.begin() as connection:
        raw = await connection.get_raw_connection()
        await connection.execute(text("SELECT 1"))
        await raw.driver_connection.execute(snapshot()[0].sql)
        await connection.execute(text("INSERT INTO public.sessions(id,user_id) VALUES ('old','user')"))
        await connection.execute(
            text("INSERT INTO public.messages(session_id,role,content) VALUES ('old','user','kept')")
        )
    denied = await upgrade(pg_engine)
    assert denied.reason == "baseline_required" and denied.confirmed == []
    accepted = await upgrade(pg_engine, baseline=True)
    assert accepted.reason == "ok" and accepted.confirmed == [1]
    async with pg_engine.begin() as connection:
        assert (await connection.execute(text("SELECT content FROM public.messages"))).scalar_one() == "kept"
        assert (
            await connection.execute(
                text("INSERT INTO public.messages(role,content) VALUES ('user','next') RETURNING id")
            )
        ).scalar_one() == 2


@pytest.mark.parametrize(
    "change",
    [
        "ALTER TABLE public.sessions ADD COLUMN extra text",
        "DROP INDEX public.ix_messages_session_id",
        "ALTER TABLE public.messages ALTER COLUMN meta TYPE jsonb USING meta::jsonb",
        "ALTER TABLE public.messages ADD CONSTRAINT extra_check CHECK (id > 0)",
        "ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY",
        "ALTER SEQUENCE public.messages_id_seq INCREMENT BY 2",
    ],
)
async def test_incompatible_existing_schema_is_rejected(pg_engine, change):
    async with pg_engine.begin() as connection:
        await connection.execute(text("SELECT 1"))
        raw = await connection.get_raw_connection()
        await raw.driver_connection.execute(snapshot()[0].sql)
        await connection.execute(text(change))
    result = await upgrade(pg_engine, baseline=True)
    assert result.reason == "schema_mismatch" and result.confirmed == []


async def test_lagging_sequence_is_rejected_without_advancing_it(pg_engine):
    async with pg_engine.begin() as connection:
        await connection.execute(text("SELECT 1"))
        raw = await connection.get_raw_connection()
        await raw.driver_connection.execute(snapshot()[0].sql)
        await connection.execute(text("INSERT INTO public.messages(id,role,content) VALUES (10,'user','kept')"))
    result = await upgrade(pg_engine, baseline=True)
    assert result.reason == "schema_mismatch"
    async with pg_engine.connect() as connection:
        row = (await connection.execute(text("SELECT last_value,is_called FROM public.messages_id_seq"))).one()
        assert tuple(row) == (1, False)


async def test_version_registration_failure_rolls_back_all_ddl(pg_engine, monkeypatch):
    monkeypatch.setattr(module, "_REGISTER_SQL", "INSERT INTO public.schema_versions(no_such_column) VALUES (1)")
    result = await upgrade(pg_engine)
    assert result.reason == "schema_mismatch" and result.confirmed == []
    async with pg_engine.connect() as connection:
        for name in ("sessions", "messages", "schema_versions"):
            assert (
                await connection.execute(text("SELECT to_regclass(:name)"), {"name": f"public.{name}"})
            ).scalar() is None


async def test_later_file_failure_preserves_committed_prefix(pg_engine, tmp_path):
    (tmp_path / snapshot()[0].name).write_bytes((ROOT / "migrations" / snapshot()[0].name).read_bytes())
    (tmp_path / "0002_failure.sql").write_text("SELECT 1/0;\n", encoding="utf-8", newline="\n")
    result = await upgrade(pg_engine, migrations=module.load_migrations(tmp_path))
    assert result.reason == "migration_database_error" and result.confirmed == [1]
    async with pg_engine.connect() as connection:
        assert (await connection.execute(text("SELECT version FROM public.schema_versions"))).scalars().all() == [1]


async def test_baseline_flag_cannot_bypass_bad_history(pg_engine):
    assert (await upgrade(pg_engine)).reason == "ok"
    async with pg_engine.begin() as connection:
        await connection.execute(text("UPDATE public.schema_versions SET checksum='changed'"))
    result = await upgrade(pg_engine, baseline=True)
    assert result.reason == "schema_mismatch" and result.confirmed == []


async def test_readonly_transaction_is_not_ready(pg_engine):
    assert (await upgrade(pg_engine)).reason == "ok"
    async with pg_engine.begin() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(module.MigrationError, match="permission_denied"):
            await module.check_schema(connection, snapshot(), deadline=deadline())


async def test_competing_migrator_fails_without_waiting(pg_engine):
    assert (await upgrade(pg_engine)).reason == "ok"
    async with pg_engine.begin() as holder:
        await holder.execute(text("LOCK TABLE public.schema_versions IN EXCLUSIVE MODE"))
        async with pg_engine.begin() as competitor:
            with pytest.raises(module.MigrationError, match="migration_locked"):
                await module.prepare_schema(competitor, snapshot(), deadline=deadline())


async def test_real_commit_with_lost_response_remains_unknown(pg_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncTransaction

    original = AsyncTransaction.commit

    async def lost_response(transaction):
        await original(transaction)
        raise OSError("simulated lost acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncTransaction, "commit", lost_response)
        with pytest.warns(SAWarning, match="transaction already deassociated"):
            result = await upgrade(pg_engine)
    assert result.reason == "commit_unknown" and result.confirmed == [] and result.uncertain_version == 1
    async with pg_engine.connect() as connection:
        assert (await connection.execute(text("SELECT version FROM public.schema_versions"))).scalar_one() == 1
    assert (await upgrade(pg_engine)).confirmed == []


async def test_real_cli_creates_schema_and_repeat_is_noop(pg_engine):
    env = os.environ.copy()
    env["DATABASE_URL"] = pg_engine.url.render_as_string(hide_password=False)
    for entry in ("scripts.migrate", "scripts.init_db"):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            entry,
            cwd=ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        report = json.loads(stdout)
        assert process.returncode == 0 and report["reason"] == "ok", report["reason"]
        assert report["confirmed_versions"] == ([1] if entry == "scripts.migrate" else [])
        assert report["cleanup_complete"] and not report["commit_outcome_unknown"]
        assert b"Traceback" not in stderr


async def test_incoming_foreign_key_cannot_change_managed_delete_semantics(pg_engine):
    from app.infrastructure import database_schema

    assert (await upgrade(pg_engine)).reason == "ok"
    async with pg_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text(
                    "CREATE TABLE public.f04_external_reference "
                    "(session_id varchar(36) REFERENCES public.sessions(id) ON DELETE RESTRICT)"
                )
            )
            with pytest.raises(database_schema.SchemaError, match="schema_mismatch"):
                await database_schema.validate_session_tables(connection, deadline(), baseline=True)
        finally:
            await transaction.rollback()


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE public.sessions(id varchar(36) PRIMARY KEY)",
        "CREATE TABLE public.schema_versions(version text PRIMARY KEY)",
    ],
)
async def test_partial_or_invalid_version_schema_is_not_repaired(pg_engine, ddl):
    async with pg_engine.begin() as connection:
        await connection.execute(text(ddl))
    result = await upgrade(pg_engine, baseline=True)
    assert result.reason == "schema_mismatch" and result.confirmed == []


async def test_valid_empty_version_table_can_upgrade(pg_engine):
    async with pg_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE public.schema_versions(version integer PRIMARY KEY,name text NOT NULL,"
                "checksum text NOT NULL,applied_at timestamptz NOT NULL)"
            )
        )
    result = await upgrade(pg_engine)
    assert result.reason == "ok" and result.confirmed == [1]


async def test_first_creation_contention_is_bounded_and_rolls_back(pg_engine):
    async with pg_engine.connect() as holder:
        transaction = await holder.begin()
        try:
            await module.prepare_schema(holder, snapshot(), deadline=deadline())
            async with pg_engine.begin() as competitor:
                with pytest.raises(module.MigrationError, match="timeout"):
                    await module.prepare_schema(
                        competitor,
                        snapshot(),
                        deadline=asyncio.get_running_loop().time() + 0.15,
                    )
                # 错误被断言消费后仍须回滚失效事务，不能让 begin 上下文尝试提交。
                await competitor.rollback()
        finally:
            await transaction.rollback()
    result = await upgrade(pg_engine)
    assert result.reason == "ok" and result.confirmed == [1]
