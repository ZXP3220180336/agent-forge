"""唯一迁移序列的文件/历史校验和事务内执行核心。

调用方持有连接与逐文件事务，负责最终提交、回滚、关闭及有界收尾。
本模块不创建资源或后台任务，不提供完整 schema readiness 证明。
"""

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from asyncpg import PostgresError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

_FILENAME = re.compile(r"([0-9]{4})_([a-z][a-z0-9]*(?:_[a-z0-9]+)*)\.sql")
_HISTORY_SQL = "SELECT version, name, checksum FROM public.schema_versions ORDER BY version"
_LOCK_SQL = "LOCK TABLE public.schema_versions IN EXCLUSIVE MODE NOWAIT"
_REGISTER_SQL = (
    "INSERT INTO public.schema_versions (version, name, checksum, applied_at) "
    "VALUES (:version, :name, :checksum, CURRENT_TIMESTAMP)"
)


class MigrationError(RuntimeError):
    """只携带稳定原因码，不包含文件内容、路径或数据库异常文本。"""


@dataclass(frozen=True, kw_only=True)
class Migration:
    """单次发现的不可变文件快照；name 为完整文件名，checksum 为原始字节 SHA-256。"""

    version: int
    name: str
    checksum: str
    sql: str = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class AppliedMigration:
    """数据库已登记历史的身份字段；applied_at 不参与文件一致性判断。"""

    version: int
    name: str
    checksum: str


def load_migrations(directory: Path) -> tuple[Migration, ...]:
    """发现平铺 SQL 文件并完整验证；不规范化内容，不扫描工具专属子序列。"""
    migrations = []
    try:
        for path in directory.iterdir():  # 读取本地所有迁移文件
            if path.suffix.lower() != ".sql":
                continue
            match = _FILENAME.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise MigrationError("migration_filename_invalid")
            raw = path.read_bytes()
            migrations.append(
                Migration(
                    version=int(match[1]),
                    name=path.name,
                    checksum=hashlib.sha256(raw).hexdigest(),
                    sql=_decode_sql(raw),
                )
            )
    except OSError:
        raise MigrationError("migration_files_unavailable") from None
    result = tuple(sorted(migrations, key=lambda migration: migration.version))  # 按版本号排好序
    _validate_migrations(result)
    return result


def validate_history(
    migrations: Sequence[Migration],
    history: Sequence[AppliedMigration],
    *,
    require_head: bool = False,
) -> int:
    """校验整个本地序列和已登记历史，返回前缀长度；readiness 必须要求精确 head。"""
    _validate_migrations(migrations)

    if len(history) > len(migrations):
        # 数据库里的历史记录数，**不能比本地文件多**——
        # 多了说明数据库跑过本地没有的迁移，结构不兼容，直接报错，对应「完整历史拒绝」。
        raise MigrationError("schema_mismatch")

    for expected, applied in enumerate(history, start=1):
        if type(applied.version) is not int or applied.version != expected:
            # 数据库里的版本号必须连续从 1 开始，不能断号
            raise MigrationError("schema_mismatch")

        local = migrations[expected - 1]
        # 每一条历史的名称、校验和必须和本地文件完全一致
        if applied.name != local.name or applied.checksum != local.checksum:
            raise MigrationError("schema_mismatch")

    # `require_head=True` 时，必须完全对齐（历史数 = 本地文件数），差一个都不行，用于严格的就绪检查。
    if require_head and len(history) != len(migrations):
        raise MigrationError("schema_mismatch")

    return len(history)


async def check_version_history(
    connection: AsyncConnection,
    migrations: Sequence[Migration],
    *,
    deadline: float,
) -> int:
    """只读核对完整历史与精确 head；不锁表、不写入、不代替结构和权限检查。"""
    snapshot = tuple(migrations)
    _validate_migrations(snapshot)
    try:
        _guard(deadline)
        async with asyncio.timeout_at(deadline):
            history = await _read_history(connection, deadline)
            version = validate_history(snapshot, history, require_head=True)
            _guard(deadline)
            return version
    except TimeoutError:
        raise MigrationError("timeout") from None
    except (SQLAlchemyError, PostgresError, OSError) as error:
        raise _database_failure(error) from None


async def apply_next_migration(
    connection: AsyncConnection,
    migrations: Sequence[Migration],
    *,
    deadline: float,
) -> int | None:
    """在调用方的事务内执行并登记下一文件，返回版本号；已到 head 返回 None。

    前置条件：版本表及受管 schema 已经通过对应结构/基线门禁，连接独占，
    每次调用单独事务。返回版本号仅表示事务内已执行，不代表提交成功。
    异常/取消后调用方必须回滚并有界清理，不得提交或直接重试本事务。
    """
    snapshot = tuple(migrations)
    _validate_migrations(snapshot)

    # 必须在事务内执行，不允许自动提交模式。
    # 保证 SQL 执行和版本登记在同一个事务里，要么全成功要么全回滚，对应「失败回滚」。
    if not connection.in_transaction():
        raise MigrationError("transaction_required")
    # sync_connection 是可选属性（异步方言可以不提供同步连接）。读不到执行选项就无法证明
    # 不是 AUTOCOMMIT，按 fail closed 拒绝，避免整批 SQL 脱离调用方事务单独提交。
    sync_connection = connection.sync_connection
    if sync_connection is None or sync_connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        raise MigrationError("transaction_required")

    try:
        _guard(deadline)
        async with asyncio.timeout_at(deadline):
            # 排他锁锁住版本表，别的迁移同时进来会直接拿锁失败，防止并发冲突。`NOWAIT` 不等待，避免阻塞。
            await connection.execute(text(_LOCK_SQL))

            # 锁表之后再读一次历史、再校验一遍，防止锁表之前别人偷偷改了版本。
            # 确认当前进度，算出下一个要执行的迁移。
            history = await _read_history(connection, deadline)
            count = validate_history(snapshot, history)

            _guard(deadline)
            if count == len(snapshot):  # 已经是最新就返回，对应前面描述的已到 head 返回 None
                return None
            _guard(deadline)
            raw = await connection.get_raw_connection()
            _guard(deadline)

            # ---原生驱动执行整批 SQL，不按分号切割---
            # SQLAlchemy 的 asyncpg 方言对 cursor.execute 一律 prepare（扩展协议，只容单条语句）；
            # 不带参数调用 asyncpg 原生 execute 才走简单查询协议，可整批执行。
            driver = raw.driver_connection  # 拿底层 asyncpg 原生驱动
            # driver_connection 是可选属性（池代理不保证暴露驱动连接）。拿不到就无法核验
            # 物理事务，按 fail closed 拒绝，避免整批 SQL 在无法证明的事务里执行。
            if driver is None or not driver.is_in_transaction():
                raise MigrationError("transaction_required")

            migration = snapshot[count]  # 获取本次要执行的迁移
            # 不关闭 raw proxy、不另开 asyncpg 事务；整批 SQL 与登记共用上层事务。
            await driver.execute(migration.sql, timeout=_guard(deadline))

            _guard(deadline)
            if not driver.is_in_transaction():
                raise MigrationError("transaction_lost")

            # 把本次迁移的版本、名称、校验和插入 `schema_versions` 表，更新迁移历史。
            await connection.execute(
                text(_REGISTER_SQL),
                {"version": migration.version, "name": migration.name, "checksum": migration.checksum},
            )
            _guard(deadline)

            return migration.version
    except TimeoutError:
        raise MigrationError("timeout") from None
    except (SQLAlchemyError, PostgresError, OSError) as error:
        raise _database_failure(error) from None


def _decode_sql(raw: bytes) -> str:
    """严格校验编码: 必须是纯 UTF-8，不能有 BOM 头、不能有回车符、不能是空文件"""
    try:
        sql = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MigrationError("migration_encoding_invalid") from None
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or b"\0" in raw or not sql.strip():
        raise MigrationError("migration_encoding_invalid")
    return sql


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    """二次校验序列完整性"""
    if not migrations:
        raise MigrationError("migration_files_missing")

    for expected, migration in enumerate(migrations, start=1):
        if type(migration.version) is not int or migration.version != expected:
            # 版本必须连续从 1 开始：不能跳号、不能缺号、不能重复，比如不能有 0001 之后直接是 0003
            raise MigrationError("migration_sequence_invalid")

        match = _FILENAME.fullmatch(migration.name)
        if match is None or int(match[1]) != migration.version:
            # 文件名里的版本号和实际 version 字段必须一致
            raise MigrationError("migration_filename_invalid")

        # 内容重新编码后再算校验和，必须和原来的完全匹配
        try:
            raw = migration.sql.encode("utf-8")
        except UnicodeEncodeError:
            raise MigrationError("migration_encoding_invalid") from None
        _decode_sql(raw)
        if hashlib.sha256(raw).hexdigest() != migration.checksum:
            raise MigrationError("migration_checksum_invalid")


def _guard(deadline: float) -> float:
    if isinstance(deadline, bool) or not math.isfinite(deadline):
        raise MigrationError("deadline_invalid")
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _database_failure(error: Exception) -> MigrationError:
    original = getattr(error, "orig", error)
    sqlstate = getattr(original, "sqlstate", None)
    codes = {
        "42P01": "schema_missing",
        "42703": "schema_mismatch",
        "42501": "permission_denied",
        "25006": "permission_denied",
        "55P03": "migration_locked",
        "25P01": "transaction_required",
        "57014": "timeout",
    }
    return MigrationError(codes.get(sqlstate, "migration_database_error"))


async def _read_history(connection: AsyncConnection, deadline: float) -> tuple[AppliedMigration, ...]:
    _guard(deadline)
    result = await connection.execute(text(_HISTORY_SQL))
    _guard(deadline)
    return tuple(
        AppliedMigration(version=row["version"], name=row["name"], checksum=row["checksum"])
        for row in result.mappings().all()
    )
