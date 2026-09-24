"""唯一迁移序列的文件/历史校验、事务内执行与离线命令 Owner。

核心函数借用连接与事务；MigrationCommand 负责逐文件提交、回滚与资源收尾。
CLI 父进程监督物理退出期限；本模块不提供完整 schema readiness 证明。
"""

import asyncio
import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from asyncpg import PostgresError
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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


# F04 将提供唯一受信检查器；CLI 不提供跳过结构/基线门禁的选项。
SchemaPreparer = Callable[..., Awaitable[int | None]]
SCHEMA_PREPARER: SchemaPreparer | None = None

MIGRATION_REASONS = frozenset(
    {
        "ok",
        "starting",
        "schema_gate_unavailable",
        "configuration_invalid",
        "internal_error",
        "migration_files_unavailable",
        "migration_files_missing",
        "migration_filename_invalid",
        "migration_sequence_invalid",
        "migration_encoding_invalid",
        "migration_checksum_invalid",
        "schema_missing",
        "schema_mismatch",
        "permission_denied",
        "migration_locked",
        "transaction_required",
        "transaction_lost",
        "deadline_invalid",
        "timeout",
        "migration_database_error",
        "commit_unknown",
        "cleanup_incomplete",
        "cancelled",
        "worker_failed",
        "worker_protocol_error",
    }
)


@dataclass(frozen=True, kw_only=True)
class MigrationTimeouts:
    """命令预算由 Settings 注入；数据库连接及命令超时在引擎构造时注入。"""

    file_timeout_seconds: float
    total_timeout_seconds: float
    cleanup_timeout_seconds: float


@dataclass
class MigrationProgress:
    """进度跟踪与跨进程播报：保留已确认版本和未确认提交；进程消息只传白名单事实。"""

    confirmed: list[int] = field(default_factory=list)
    uncertain_version: int | None = None
    reason: str = "starting"
    cleanup_complete: bool = False
    forced_termination: bool = False
    channel: Connection | None = field(default=None, repr=False)  # 跨进程通信管道

    def commit_started(self, version: int) -> None:
        """发送成功后才允许调用 commit；断连不能解释为尚未写入。"""
        # 准备提交事务前调用，标记这个版本进入「不确定」状态，同时给父进程发消息
        self.uncertain_version = version
        self._send(f"commit_started|{version}")

    def commit_confirmed(self, version: int) -> None:
        """提交成功、收到数据库确认后调用：先保存本地成功响应事实，再向监督进程报告。"""
        self.confirmed.append(version)
        self.uncertain_version = None
        self._send(f"committed|{version}")

    def finish(self) -> None:
        """报告资源关闭结果；父进程还必须确认 worker 已正常退出。"""
        self._send(f"finished|{self.reason}|{int(self.cleanup_complete)}")

    def _send(self, message: str) -> None:
        if self.channel is not None:
            self.channel.send_bytes(message.encode("ascii"))


class MigrationCommand:
    """离线命令的唯一资源 Owner；物理退出上限由 CLI 父进程监督。"""

    def __init__(
        self,
        engine: AsyncEngine,
        migrations: Sequence[Migration],
        *,
        timeouts: MigrationTimeouts,
        prepare_schema: SchemaPreparer | None,
        baseline_existing: bool = False,
        progress: MigrationProgress | None = None,
    ) -> None:
        self._engine = engine
        self._migrations = tuple(migrations)
        self._timeouts = timeouts
        self._prepare_schema = prepare_schema
        self._baseline_existing = baseline_existing
        self.progress = progress if progress is not None else MigrationProgress()
        self._connection: AsyncConnection | None = None
        self._started = False
        self._transaction = None
        self._drivers: list[Any] = []
        self._used = False
        event.listen(engine.sync_engine, "connect", self._track_driver, insert=True)

    def _track_driver(self, dbapi_connection: Any, record: Any) -> None:
        # 每创建一个底层驱动连接就记下来
        self._drivers.append(dbapi_connection.driver_connection)

    async def run(self, *, deadline: float) -> MigrationProgress:
        """逐文件提交；失败保留确认前缀，取消收尾后继续传播，不自动重试。"""
        if self._used:
            # 一次性 Owner 被复用是调用方缺陷，按未知程序错误上报，不伪装成可恢复的数据库原因码。
            raise MigrationError("internal_error")
        self._used = True
        loop = asyncio.get_running_loop()
        end = min(deadline, loop.time() + self._timeouts.total_timeout_seconds)
        cleanup_end = end
        work_end = end - min(self._timeouts.cleanup_timeout_seconds, max(0, end - loop.time()) / 2)

        try:
            if self._prepare_schema is None:
                raise MigrationError("schema_gate_unavailable")
            _validate_migrations(self._migrations)
            _guard(work_end)
            async with asyncio.timeout_at(work_end):
                self._connection = self._engine.connect()
                await self._connection.start()
                self._started = True
                _guard(work_end)
                # 每次至少确认一个新版本，另留一次无待执行文件的核验；无无限循环。
                for _ in range(len(self._migrations) + 1):
                    _guard(work_end)

                    file_end = min(work_end, loop.time() + self._timeouts.file_timeout_seconds)
                    cleanup_end = file_end
                    operation_end = file_end - min(
                        self._timeouts.cleanup_timeout_seconds, max(0, file_end - loop.time()) / 2
                    )

                    async with asyncio.timeout_at(operation_end):
                        self._transaction = await self._connection.begin()
                        _guard(operation_end)

                        # 调用门禁函数检查数据库状态，处理基线接管
                        baseline = await self._prepare_schema(
                            self._connection,
                            self._migrations,
                            baseline_existing=self._baseline_existing,
                            deadline=operation_end,
                        )
                        _guard(operation_end)
                        if baseline is not None:
                            if not self._baseline_existing or type(baseline) is not int or baseline != 1:
                                raise MigrationError("internal_error")
                            # 基线模式：直接把当前数据库状态登记为基线版本，不用真的执行 SQL。
                            version = baseline
                        else:
                            # 正常模式：调用底层函数执行下一个迁移文件。
                            version = await apply_next_migration(
                                self._connection,
                                self._migrations,
                                deadline=operation_end,
                            )
                        _guard(operation_end)

                        # 到头判断与重复校验
                        if version is None:
                            # 返回 `None` 说明已经到最新版本了，没有迁移要跑了，回滚这个只读校验事务，正常结束。
                            await self._transaction.rollback()
                            self._transaction = None
                            _guard(operation_end)
                            self.progress.reason = "ok"
                            break
                        if version in self.progress.confirmed:
                            # 如果返回的版本已经在「已确认」列表里，说明出现了重复迁移，直接报结构不匹配。
                            raise MigrationError("schema_mismatch")

                        # 提交
                        _guard(operation_end)
                        # 先发 commit 再登记未确认版本：此前超时属"未发出提交"，不得记成提交结果未知。
                        self.progress.commit_started(version)
                        await self._transaction.commit()  # 真正提交
                        self._transaction = None
                        self.progress.commit_confirmed(version)  # 确认成功
                        _guard(operation_end)

                    cleanup_end = end
                else:
                    raise MigrationError("schema_mismatch")
        except asyncio.CancelledError:
            self.progress.reason = "commit_unknown" if self.progress.uncertain_version else "cancelled"
            raise
        except Exception as error:  # noqa: BLE001 — 命令错误出口脱敏；未知缺陷报告 internal_error。
            if self.progress.uncertain_version is not None:
                self.progress.reason = "commit_unknown"
            elif isinstance(error, MigrationError) and str(error) in MIGRATION_REASONS:
                # 是已知迁移错误 → 直接用原因码
                self.progress.reason = str(error)
            elif isinstance(error, TimeoutError):
                self.progress.reason = "timeout"
            elif isinstance(error, (SQLAlchemyError, PostgresError, OSError)):
                # 数据库错误 → 转成对应原因码
                self.progress.reason = str(_database_failure(error))
            else:
                self.progress.reason = "internal_error"
        finally:
            await self._cleanup(min(cleanup_end, end, loop.time() + self._timeouts.cleanup_timeout_seconds))
        return self.progress

    async def _cleanup(self, deadline: float) -> None:
        def check_deadline() -> None:
            # 已收到的业务取消不禁止必要回滚；重复取消仍由 await 正常传播。
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError

        complete = True
        try:
            async with asyncio.timeout_at(deadline):
                check_deadline()
                if self._transaction is not None:
                    # 优先回滚未提交的事务
                    await self._transaction.rollback()
                    self._transaction = None
                check_deadline()
                if self._started:
                    # 正常关闭数据库连接
                    assert self._connection is not None
                    await self._connection.close()
                check_deadline()
                # 正常关闭引擎
                await self._engine.dispose()
                check_deadline()
                # 检查所有驱动连接都已关闭
                complete = all(driver.is_closed() for driver in self._drivers)
        except asyncio.CancelledError:
            complete = False
            raise
        except Exception:  # noqa: BLE001 — 收尾失败不覆盖已确认版本、提交未知或主失败。
            complete = False
        finally:
            if not complete:
                for driver in self._drivers:
                    if not driver.is_closed():
                        try:
                            driver.terminate()  # 失败则暴力 terminate 所有驱动
                        except Exception:  # noqa: BLE001 — 不回显驱动异常，关闭仍报告未完成。
                            complete = False
            self.progress.cleanup_complete = complete
            if not complete and self.progress.reason == "ok":
                self.progress.reason = "cleanup_incomplete"
