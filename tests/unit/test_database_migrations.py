"""迁移核心验收；事务 fake 不替代真实 PostgreSQL 原子性验收。"""

import asyncio
import hashlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from asyncpg import PostgresError
from sqlalchemy.exc import DBAPIError

import app.infrastructure.database_migrations as migration_module
from app.infrastructure.database_migrations import (
    AppliedMigration,
    Migration,
    MigrationError,
    apply_next_migration,
    check_version_history,
    load_migrations,
    validate_history,
)


def test_load_migrations_rejects_gaps_before_execution(tmp_path: Path) -> None:
    """执行前必须拒绝缺号序列。"""
    (tmp_path / "0001_first.sql").write_bytes(b"SELECT 1;\n")
    (tmp_path / "0003_third.sql").write_bytes(b"SELECT 3;\n")
    with pytest.raises(MigrationError, match="migration_sequence_invalid"):
        load_migrations(tmp_path)


@pytest.fixture
def migrations(tmp_path: Path) -> tuple[Migration, ...]:
    """文件内容包含注释、字符串和 dollar quoting 内分号。"""
    (tmp_path / "0002_second.sql").write_bytes(b"SELECT 2;\n")
    (tmp_path / "0001_first.sql").write_bytes(
        "-- 注释;\nCREATE TABLE demo (value text);\nINSERT INTO demo VALUES ('a;b'), ($$c;d$$);\n".encode()
    )
    return load_migrations(tmp_path)


def applied(migration: Migration) -> AppliedMigration:
    """生成与本地快照一致的版本登记。"""
    return AppliedMigration(version=migration.version, name=migration.name, checksum=migration.checksum)


def deadline() -> float:
    """使用与 asyncio timeout 相同的单调时钟。"""
    return asyncio.get_running_loop().time() + 1


class FakeConnection:
    """显式记录调用顺序与事务前置条件；不模拟 PostgreSQL 原子性。"""

    def __init__(self, history: tuple[AppliedMigration, ...] = ()) -> None:
        self.history = list(history)
        self.calls: list[str] = []
        self.registered: list[dict[str, Any]] = []
        self.logical_transaction = True
        self.physical_transaction = True
        self.options: dict[str, Any] = {}
        self.hook = AsyncMock()
        self.driver: Any = SimpleNamespace(
            execute=AsyncMock(side_effect=self._execute_batch),
            is_in_transaction=lambda: self.physical_transaction,
        )
        self.sync_connection = SimpleNamespace(get_execution_options=lambda: self.options)
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.close = AsyncMock()

    def in_transaction(self) -> bool:
        return self.logical_transaction

    async def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        if sql.startswith("LOCK TABLE"):
            stage = "lock"
            assert sql == "LOCK TABLE public.schema_versions IN EXCLUSIVE MODE NOWAIT"
        elif sql.startswith("SELECT"):
            stage = "history"
            assert sql == "SELECT version, name, checksum FROM public.schema_versions ORDER BY version"
        else:
            stage = "register"
            assert ":version" in sql and "CURRENT_TIMESTAMP" in sql
        self.calls.append(stage)
        await self.hook(stage)
        if stage == "register":
            self.registered.append(parameters)
            self.history.append(AppliedMigration(**parameters))
        rows = [{"version": row.version, "name": row.name, "checksum": row.checksum} for row in self.history]
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    async def get_raw_connection(self) -> Any:
        self.calls.append("raw")
        await self.hook("raw")
        return SimpleNamespace(driver_connection=self.driver)

    async def _execute_batch(self, sql: str, *, timeout: float) -> None:
        self.calls.append("batch")
        assert timeout > 0
        await self.hook("batch")


def test_load_preserves_original_bytes_and_filename(migrations: tuple[Migration, ...], tmp_path: Path) -> None:
    assert [item.version for item in migrations] == [1, 2]
    first = migrations[0]
    assert first.name == "0001_first.sql"
    original = (tmp_path / first.name).read_bytes()
    assert first.sql.encode() == original
    assert first.checksum == hashlib.sha256(original).hexdigest()
    (tmp_path / first.name).write_bytes(b"SELECT 999;\n")
    assert first.sql.encode() == original
    with pytest.raises(FrozenInstanceError):
        first.sql = "SELECT 999;"
    assert first.sql not in repr(first)


@pytest.mark.parametrize("filename", ["001_first.sql", "0001_First.sql", "0001_first.SQL", "bad.sql", "0001_.sql"])
def test_illegal_names_are_rejected(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_bytes(b"SELECT 1;\n")
    with pytest.raises(MigrationError, match="migration_filename_invalid"):
        load_migrations(tmp_path)


@pytest.mark.parametrize("names", [("0000_first.sql",), ("0002_second.sql",), ("0001_first.sql", "0001_other.sql")])
def test_invalid_sequences_are_rejected(tmp_path: Path, names: tuple[str, ...]) -> None:
    for name in names:
        (tmp_path / name).write_bytes(b"SELECT 1;\n")
    with pytest.raises(MigrationError, match="migration_sequence_invalid"):
        load_migrations(tmp_path)


@pytest.mark.parametrize("content", [b"", b" \n", b"\xff", b"\xef\xbb\xbfSELECT 1;\n", b"SELECT 1;\r\n", b"\0"])
def test_invalid_content_is_rejected_without_normalization(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "0001_first.sql").write_bytes(content)
    with pytest.raises(MigrationError, match="migration_encoding_invalid"):
        load_migrations(tmp_path)


def test_missing_and_empty_directories_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="migration_files_unavailable"):
        load_migrations(tmp_path / "SECRET_PATH")
    with pytest.raises(MigrationError, match="migration_files_missing"):
        load_migrations(tmp_path)


def test_sql_directory_is_not_a_migration(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").mkdir()
    with pytest.raises(MigrationError, match="migration_filename_invalid"):
        load_migrations(tmp_path)


def test_non_sql_documentation_is_ignored(migrations: tuple[Migration, ...], tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("说明", encoding="utf-8")
    assert load_migrations(tmp_path) == migrations


@pytest.mark.parametrize("length", [0, 1, 2])
def test_history_accepts_only_valid_prefix(migrations: tuple[Migration, ...], length: int) -> None:
    history = tuple(applied(item) for item in migrations[:length])
    assert validate_history(migrations, history) == length
    if length < len(migrations):
        with pytest.raises(MigrationError, match="schema_mismatch"):
            validate_history(migrations, history, require_head=True)
    else:
        assert validate_history(migrations, history, require_head=True) == 2


@pytest.mark.parametrize("case", ["gap", "duplicate", "unknown", "name", "checksum", "bool", "out_of_order"])
def test_rejects_corrupt_full_history(migrations: tuple[Migration, ...], case: str) -> None:
    first, second = (applied(item) for item in migrations)
    histories = {
        "gap": (second,),
        "duplicate": (first, first),
        "unknown": (first, second, replace(second, version=3)),
        "name": (replace(first, name="0001_renamed.sql"), second),
        "checksum": (replace(first, checksum="0" * 64), second),
        "bool": (replace(first, version=True), second),
        "out_of_order": (second, first),
    }
    with pytest.raises(MigrationError, match="schema_mismatch"):
        validate_history(migrations, histories[case])


async def test_invalid_pending_file_blocks_all_database_work(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()
    corrupt = (migrations[0], replace(migrations[1], sql="SELECT 'changed';"))
    with pytest.raises(MigrationError, match="migration_checksum_invalid"):
        await apply_next_migration(connection, corrupt, deadline=deadline())
    assert connection.calls == []


async def test_version_check_is_read_only_and_requires_exact_head(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection(tuple(applied(item) for item in migrations))
    assert await check_version_history(connection, migrations, deadline=deadline()) == 2
    assert connection.calls == ["history"]
    connection.history.pop()
    with pytest.raises(MigrationError, match="schema_mismatch"):
        await check_version_history(connection, migrations, deadline=deadline())
    assert not connection.registered


async def test_whole_sql_batch_and_registration_share_the_owned_transaction(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()
    assert await apply_next_migration(connection, migrations, deadline=deadline()) == 1
    assert connection.calls == ["lock", "history", "raw", "batch", "register"]
    args, kwargs = connection.driver.execute.call_args
    assert args == (migrations[0].sql,)
    assert 0 < kwargs["timeout"] <= 1
    assert connection.registered == [{"version": 1, "name": migrations[0].name, "checksum": migrations[0].checksum}]
    connection.commit.assert_not_awaited()
    connection.rollback.assert_not_awaited()
    connection.close.assert_not_awaited()
    assert await apply_next_migration(connection, migrations, deadline=deadline()) == 2
    assert await apply_next_migration(connection, migrations, deadline=deadline()) is None
    assert connection.driver.execute.await_count == 2


async def test_history_is_reread_after_lock_not_cached(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()

    async def peer_advances(stage: str) -> None:
        if stage == "lock":
            connection.history = [applied(migrations[0])]

    connection.hook.side_effect = peer_advances
    assert await apply_next_migration(connection, migrations, deadline=deadline()) == 2
    assert connection.driver.execute.call_args.args == (migrations[1].sql,)


async def test_corrupt_existing_history_prevents_sql(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection((replace(applied(migrations[0]), checksum="changed"),))
    with pytest.raises(MigrationError, match="schema_mismatch"):
        await apply_next_migration(connection, migrations, deadline=deadline())
    assert connection.calls == ["lock", "history"]


@pytest.mark.parametrize("case", ["no_transaction", "autocommit", "logical_only", "no_sync_connection"])
async def test_requires_a_real_transaction(migrations: tuple[Migration, ...], case: str) -> None:
    connection = FakeConnection()
    if case == "no_transaction":
        connection.logical_transaction = False
    elif case == "autocommit":
        connection.options["isolation_level"] = "AUTOCOMMIT"
    elif case == "no_sync_connection":
        # 读不到执行选项就无法证明不是 AUTOCOMMIT，按 fail closed 拒绝，不能落到 AttributeError
        connection.sync_connection = None
    else:
        connection.physical_transaction = False
    with pytest.raises(MigrationError, match="transaction_required"):
        await apply_next_migration(connection, migrations, deadline=deadline())
    connection.driver.execute.assert_not_awaited()
    assert not connection.registered


async def test_missing_driver_connection_cannot_prove_a_transaction(migrations: tuple[Migration, ...]) -> None:
    """拿不到驱动连接就无法核验物理事务，按 fail closed 拒绝且不执行整批 SQL。"""
    connection = FakeConnection()
    connection.driver = None
    with pytest.raises(MigrationError, match="transaction_required"):
        await apply_next_migration(connection, migrations, deadline=deadline())
    assert connection.calls == ["lock", "history", "raw"]
    assert not connection.registered


@pytest.mark.parametrize("stage", ["lock", "history", "raw", "batch", "register"])
async def test_cancel_propagates_without_next_step(migrations: tuple[Migration, ...], stage: str) -> None:
    connection = FakeConnection()

    async def cancel(current: str) -> None:
        if current == stage:
            raise asyncio.CancelledError

    connection.hook.side_effect = cancel
    with pytest.raises(asyncio.CancelledError):
        await apply_next_migration(connection, migrations, deadline=deadline())
    assert connection.calls[-1] == stage
    assert not connection.registered
    connection.rollback.assert_not_awaited()
    connection.close.assert_not_awaited()


@pytest.mark.parametrize("stage", ["lock", "history", "raw", "batch", "register"])
async def test_timeout_stops_without_background_work(migrations: tuple[Migration, ...], stage: str) -> None:
    connection = FakeConnection()

    async def hang(current: str) -> None:
        if current == stage:
            await asyncio.Event().wait()

    connection.hook.side_effect = hang
    with pytest.raises(MigrationError, match="timeout"):
        await apply_next_migration(connection, migrations, deadline=asyncio.get_running_loop().time() + 0.02)
    assert connection.calls[-1] == stage
    assert not connection.registered


async def test_swallowed_timeout_cannot_register_after_late_sql(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()

    async def swallow(current: str) -> None:
        if current == "batch":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

    connection.hook.side_effect = swallow
    with pytest.raises(MigrationError, match="timeout"):
        await apply_next_migration(connection, migrations, deadline=asyncio.get_running_loop().time() + 0.02)
    assert connection.calls[-1] == "batch"
    assert not connection.registered


@pytest.mark.parametrize(
    "state,reason",
    [
        ("55P03", "migration_locked"),
        ("42P01", "schema_missing"),
        ("42501", "permission_denied"),
        ("42703", "schema_mismatch"),
    ],
)
async def test_database_errors_are_sanitized(migrations: tuple[Migration, ...], state: str, reason: str) -> None:
    connection = FakeConnection()
    original = RuntimeError("SECRET_DATABASE_URL")
    original.sqlstate = state
    connection.hook.side_effect = DBAPIError("SECRET_SQL", {"value": "SECRET_VALUE"}, original)
    with pytest.raises(MigrationError, match=reason) as caught:
        await apply_next_migration(connection, migrations, deadline=deadline())
    assert "SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert not connection.registered


async def test_unknown_programming_error_is_not_recoverable(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()
    connection.hook.side_effect = ValueError("bug")
    with pytest.raises(ValueError, match="bug"):
        await apply_next_migration(connection, migrations, deadline=deadline())


async def test_expired_deadline_starts_no_sql(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()
    with pytest.raises(MigrationError, match="timeout"):
        await apply_next_migration(connection, migrations, deadline=asyncio.get_running_loop().time() - 1)
    assert connection.calls == []


async def test_transaction_lost_does_not_register(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()

    async def lost(stage: str) -> None:
        if stage == "batch":
            connection.physical_transaction = False

    connection.hook.side_effect = lost
    with pytest.raises(MigrationError, match="transaction_lost"):
        await apply_next_migration(connection, migrations, deadline=deadline())
    assert not connection.registered


@pytest.mark.parametrize("stage", ["batch", "register"])
async def test_failed_sql_or_registration_never_commits(migrations: tuple[Migration, ...], stage: str) -> None:
    connection = FakeConnection()

    async def fail(current: str) -> None:
        if current == stage:
            if stage == "batch":
                raise PostgresError("SECRET_SQL")
            raise DBAPIError("SECRET_SQL", {}, RuntimeError("SECRET_DATABASE_URL"))

    connection.hook.side_effect = fail
    with pytest.raises(MigrationError, match="migration_database_error") as caught:
        await apply_next_migration(connection, migrations, deadline=deadline())
    assert "SECRET" not in str(caught.value)
    assert connection.calls[-1] == stage
    connection.commit.assert_not_awaited()
    connection.rollback.assert_not_awaited()
    assert not connection.registered


async def test_swallowed_external_cancel_cannot_register(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()
    entered = asyncio.Event()

    async def swallow(stage: str) -> None:
        if stage == "batch":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

    connection.hook.side_effect = swallow
    task = asyncio.create_task(apply_next_migration(connection, migrations, deadline=deadline()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connection.calls[-1] == "batch"
    assert not connection.registered


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), True])
async def test_invalid_deadline_starts_no_work(migrations: tuple[Migration, ...], invalid: float) -> None:
    connection = FakeConnection()
    with pytest.raises(MigrationError, match="deadline_invalid"):
        await apply_next_migration(connection, migrations, deadline=invalid)
    with pytest.raises(MigrationError, match="deadline_invalid"):
        await check_version_history(connection, migrations, deadline=invalid)
    assert not connection.calls


async def test_read_only_history_timeout_is_bounded(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()

    async def hang(stage: str) -> None:
        await asyncio.Event().wait()

    connection.hook.side_effect = hang
    with pytest.raises(MigrationError, match="timeout"):
        await check_version_history(connection, migrations, deadline=asyncio.get_running_loop().time() + 0.02)
    assert connection.calls == ["history"]


async def test_read_only_missing_table_does_not_create_it(migrations: tuple[Migration, ...]) -> None:
    connection = FakeConnection()
    original = RuntimeError("SECRET")
    original.sqlstate = "42P01"
    connection.hook.side_effect = DBAPIError("SECRET", {}, original)
    with pytest.raises(MigrationError, match="schema_missing"):
        await check_version_history(connection, migrations, deadline=deadline())
    assert connection.calls == ["history"]


@pytest.mark.parametrize("check_only", [False, True])
async def test_history_validation_cannot_return_success_past_deadline(
    migrations: tuple[Migration, ...],
    monkeypatch: pytest.MonkeyPatch,
    check_only: bool,
) -> None:
    connection = FakeConnection(tuple(applied(item) for item in migrations))
    loop = asyncio.get_running_loop()
    original_time = loop.time
    offset = 0.0
    original_validate = validate_history

    def slow_validation(*args: Any, **kwargs: Any) -> int:
        nonlocal offset
        result = original_validate(*args, **kwargs)
        offset = 10.0  # 确定性模拟本地历史校验消耗预算，不实际睡眠。
        return result

    monkeypatch.setattr(loop, "time", lambda: original_time() + offset)
    monkeypatch.setattr(migration_module, "validate_history", slow_validation)
    operation = check_version_history if check_only else apply_next_migration
    try:
        with pytest.raises(MigrationError, match="timeout"):
            await operation(connection, migrations, deadline=deadline())
    finally:
        offset = 0.0
