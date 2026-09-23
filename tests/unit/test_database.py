"""数据库驱动与独立运行时验收；fake 不替代 DB-F06 的真实 PostgreSQL。"""

import asyncio
import logging
import socket
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import app.infrastructure.database as database_module

TEST_TIMEOUTS = database_module.DatabaseTimeouts(
    connect_timeout_seconds=0.2,
    pool_timeout_seconds=0.2,
    operation_timeout_seconds=0.2,
    probe_timeout_seconds=0.2,
    cleanup_timeout_seconds=0.05,
    shutdown_timeout_seconds=0.2,
)


def make_runtime(**overrides: Any) -> database_module.DatabaseRuntime:
    """短但有限的预算，使挂起测试不依赖真实服务。"""
    options = {
        "url": "postgresql+asyncpg://localhost/runtime_test",
        "pool_size": 1,
        "max_overflow": 0,
        "echo": False,
        "timeouts": TEST_TIMEOUTS,
    }
    options.update(overrides)
    return database_module.DatabaseRuntime(**options)


class FakeDriver:
    def __init__(self) -> None:
        self.closed = False
        self.terminations = 0

    def is_closed(self) -> bool:
        return self.closed

    def terminate(self) -> None:
        self.terminations += 1
        self.closed = True


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> SimpleNamespace:
    """模拟公开事件和连接边界，不模拟 runtime 内部状态转换。"""
    handlers = {}
    driver = FakeDriver()
    dbapi = SimpleNamespace(driver_connection=driver)
    record = object()
    statements = []

    async def start() -> None:
        handlers["connect"](dbapi, record)
        handlers["checkout"](dbapi, record, None)

    async def close() -> None:
        handlers["checkin"](dbapi, record)

    async def execute(statement: Any) -> None:
        statements.append(str(statement))

    async def dispose() -> None:
        driver.closed = True

    connection = SimpleNamespace(
        start=AsyncMock(side_effect=start),
        begin=AsyncMock(),
        execute=AsyncMock(side_effect=execute),
        scalar=AsyncMock(return_value=1),
        close=AsyncMock(side_effect=close),
    )
    # 池实现名刻意不叫 AsyncAdaptedQueuePool：脱敏过滤器必须跟着实例走，不能靠拼类名。
    # 名字按用例唯一，避免同名 logger 的既有过滤器跨用例串味（漏挂会假通过）。
    suffix = request.node.name
    sync_engine = SimpleNamespace(
        logger=logging.getLogger(f"sqlalchemy.engine.Engine.fake.{suffix}"),
        pool=SimpleNamespace(logger=logging.getLogger(f"sqlalchemy.pool.impl.FakePool.fake.{suffix}")),
    )
    engine = SimpleNamespace(
        sync_engine=sync_engine, connect=lambda: connection, dispose=AsyncMock(side_effect=dispose)
    )
    builds = []

    def build(**options: Any) -> SimpleNamespace:
        builds.append(options)
        return engine

    def listen(target: Any, name: str, callback: Any, **kwargs: Any) -> None:
        handlers[name] = callback

    class FakeSession:
        def __init__(self, runtime: Any) -> None:
            self.runtime = runtime

        async def close(self) -> None:
            self.runtime._sessions.discard(self)

    monkeypatch.setattr(database_module, "create_async_engine", build)
    monkeypatch.setattr(database_module.event, "listen", listen)
    monkeypatch.setattr(
        database_module, "async_sessionmaker", lambda *args, **kwargs: lambda: FakeSession(kwargs["runtime"])
    )
    return SimpleNamespace(
        connection=connection,
        engine=engine,
        driver=driver,
        dbapi=dbapi,
        record=record,
        statements=statements,
        handlers=handlers,
        builds=builds,
    )


async def test_init_checks_transaction_and_schema_before_opening_factory(backend: SimpleNamespace) -> None:
    checker = AsyncMock()
    runtime = make_runtime(schema_check=checker)
    try:
        status = await runtime.init()
        assert status.ready and status.schema_version == 1
        checker.assert_awaited_once_with(backend.connection)
        backend.connection.begin.assert_awaited_once()
        backend.connection.close.assert_awaited_once()
        assert backend.statements == ["SET TRANSACTION READ ONLY", "SELECT 1"]
        assert str(backend.connection.scalar.call_args.args[0]) == "SELECT max(version) FROM public.schema_versions"
        session = runtime.session_factory()
        assert session is not None
        await session.close()
        await runtime.init()
        assert len(backend.builds) == 1
        options = backend.builds[0]
        assert options["hide_parameters"] is True
        assert options["pool_pre_ping"] is True
        assert options["connect_args"] == {"timeout": 0.2, "command_timeout": 0.2}
        assert options["pool_timeout"] == 0.2
    finally:
        await runtime.dispose()


async def test_version_and_ping_alone_never_open_factory(backend: SimpleNamespace) -> None:
    runtime = make_runtime()
    try:
        assert not await runtime.ping()
        status = await runtime.init()
        assert status.reason == "schema_mismatch" and status.schema_version == 1
        assert await runtime.ping()
        assert runtime.status == status
        with pytest.raises(database_module.DatabaseRuntimeError, match="schema_mismatch"):
            _ = runtime.session_factory
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "sqlstate,reason",
    [
        ("28P01", "authentication_failed"),
        ("42501", "permission_denied"),
        ("25006", "permission_denied"),
        ("42P01", "schema_missing"),
        ("42703", "schema_mismatch"),
        ("08006", "connection_failed"),
        ("57014", "timeout"),
    ],
)
async def test_expected_failures_are_sanitized_and_connection_closed(
    backend: SimpleNamespace, sqlstate: str, reason: str
) -> None:
    error = RuntimeError("SECRET_DATABASE_URL")
    error.sqlstate = sqlstate
    backend.connection.scalar.side_effect = error
    runtime = make_runtime(schema_check=AsyncMock())
    try:
        assert (await runtime.init()).reason == reason
        backend.connection.close.assert_awaited_once()
        assert "SECRET" not in repr(runtime.status)
    finally:
        await runtime.dispose()


async def test_unknown_bug_is_not_a_recoverable_database_failure(backend: SimpleNamespace) -> None:
    backend.connection.execute.side_effect = ValueError("SECRET_DATABASE_URL")
    runtime = make_runtime()
    try:
        with pytest.raises(database_module.DatabaseRuntimeError, match="internal_error") as caught:
            await runtime.init()
        assert caught.value.__cause__ is None
        assert "SECRET" not in str(caught.value)
        backend.connection.close.assert_awaited_once()
    finally:
        await runtime.dispose()


async def test_idle_termination_never_touches_an_owned_driver(backend: SimpleNamespace) -> None:
    """DB-006：强制终止只处理空闲驱动，归还前不得强关在用事务。"""
    runtime = make_runtime()
    runtime._build_engine()
    backend.handlers["connect"](backend.dbapi, backend.record)
    assert all(owner is not None for owner in runtime._drivers.values())

    runtime._terminate_idle_drivers()
    assert backend.driver.terminations == 0
    assert not backend.driver.closed

    backend.handlers["checkin"](backend.dbapi, backend.record)
    runtime._terminate_idle_drivers()
    assert backend.driver.terminations == 1


@pytest.mark.parametrize("state", ["closing", "closed"])
async def test_connect_during_close_is_terminated_and_rejected(backend: SimpleNamespace, state: str) -> None:
    """DB-008：关闭已开始时新建的连接被终止，并以 closing 拒绝。"""
    runtime = make_runtime()
    runtime._build_engine()
    runtime._status = database_module.DatabaseStatus(state, state)

    with pytest.raises(database_module.DatabaseRuntimeError, match="closing"):
        backend.handlers["connect"](backend.dbapi, backend.record)
    assert backend.driver.terminations == 1
    assert backend.driver.closed


def test_log_redaction_follows_the_engine_instance(backend: SimpleNamespace) -> None:
    """DB-009：脱敏过滤器跟着实例的 engine / 池 logger 走，不假定具体池实现。"""
    runtime = make_runtime()
    runtime._build_engine()
    pool_logger = runtime._engine.sync_engine.pool.logger
    assert "AsyncAdaptedQueuePool" not in pool_logger.name  # 前提：实例的池实现名不是被硬编码的那个
    for target in (runtime._engine.sync_engine.logger, pool_logger):
        assert any(isinstance(item, database_module._SafeDatabaseLog) for item in target.filters)


@pytest.mark.parametrize("code", ["closing", "close_incomplete"])
def test_runtime_own_reason_codes_are_reported_verbatim(code: str) -> None:
    """DB-005：运行期自身抛出的稳定码属 D5 白名单，不降级为未知缺陷。"""
    assert database_module._reason(database_module.DatabaseRuntimeError(code)) == code


def test_untrusted_runtime_error_text_still_becomes_internal_defect() -> None:
    """白名单之外不回显：任意文本不得冒充稳定原因码。"""
    assert database_module._reason(database_module.DatabaseRuntimeError("SECRET_DATABASE_URL")) == "internal_error"


async def test_stopped_probe_keeps_close_incomplete_instead_of_internal_defect(backend: SimpleNamespace) -> None:
    """DB-005 复现：探针被停止后仍 checkout 会被拒，probe() 应返回状态而不是抛内部错误。"""
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    assert runtime.status.ready
    fixture_start = backend.connection.start.side_effect

    async def stop_then_start() -> None:
        # 模拟关闭/取消已请求，而探针吞掉取消继续建连：_checkout 复查准入即被拒。
        runtime._probe_stopped = True
        await fixture_start()

    backend.connection.start.side_effect = stop_then_start
    try:
        status = await runtime.probe()
        assert status.reason == "close_incomplete"
        assert not status.ready
    finally:
        await runtime.dispose()


async def test_half_built_engine_never_opens_admission(
    backend: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB-007：构建中途失败不得留下“引擎存在但工厂缺失”的半构建状态。"""
    runtime = make_runtime(schema_check=AsyncMock())
    real_maker = database_module.async_sessionmaker
    attempts = {"n": 0}

    def flaky_maker(*args: Any, **kwargs: Any) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("SECRET_BUILD_FAILURE")
        return real_maker(*args, **kwargs)

    monkeypatch.setattr(database_module, "async_sessionmaker", flaky_maker)
    try:
        with pytest.raises(database_module.DatabaseRuntimeError, match="internal_error") as caught:
            await runtime.init()
        assert "SECRET" not in str(caught.value)
        assert not runtime.status.ready
        # 半构建不得被下一次 init() 当成已经建好的引擎
        assert runtime._engine is None
        assert runtime._maker is None

        assert (await runtime.init()).ready
        session = runtime.session_factory()
        await session.close()
    finally:
        assert (await runtime.dispose()).state == "closed"


async def test_ping_internal_defect_neither_raises_nor_changes_admission(backend: SimpleNamespace) -> None:
    """DB-004：观测型 ping 只报告本次结果，不改写准入，也不外抛内部缺陷文本。"""
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    assert runtime.status.ready
    backend.connection.execute.side_effect = RuntimeError("SECRET_DEFECT")
    try:
        assert await runtime.ping() is False
        assert runtime.status.ready
        assert "SECRET" not in repr(runtime.status)
    finally:
        await runtime.dispose()


async def test_dispose_is_idempotent_and_cached_factory_rechecks_admission(backend: SimpleNamespace) -> None:
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    factory = runtime.session_factory
    assert (await runtime.dispose()).state == "closed"
    assert (await runtime.dispose()).state == "closed"
    assert (await runtime.init()).state == "closed"
    assert not await runtime.ping()
    with pytest.raises(database_module.DatabaseRuntimeError):
        factory()
    backend.engine.dispose.assert_awaited_once()


async def test_dispose_does_not_close_a_borrowed_business_connection(backend: SimpleNamespace) -> None:
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    record = object()
    backend.handlers["checkout"](backend.dbapi, record, None)
    assert (await runtime.dispose()).reason == "close_incomplete"
    backend.engine.dispose.assert_not_awaited()
    assert backend.driver.terminations == 0
    backend.handlers["checkin"](backend.dbapi, record)
    assert (await runtime.dispose()).state == "closed"


async def test_late_session_checkout_is_blocked_after_dispose(backend: SimpleNamespace) -> None:
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    await runtime.dispose()
    with pytest.raises(database_module.DatabaseRuntimeError):
        backend.handlers["checkout"](backend.dbapi, object(), None)


async def test_probe_timeout_rolls_back_and_does_not_run_schema_check(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()
    checker = AsyncMock()

    async def hang(statement: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    backend.connection.execute.side_effect = hang
    runtime = make_runtime(schema_check=checker)
    started = asyncio.get_running_loop().time()
    assert (await runtime.init()).reason == "timeout"
    assert asyncio.get_running_loop().time() - started < 0.6
    assert entered.is_set()
    checker.assert_not_awaited()
    backend.connection.close.assert_awaited_once()
    await runtime.dispose()


async def test_cancellation_propagates_after_bounded_cleanup(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()

    async def hang(statement: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    backend.connection.execute.side_effect = hang
    runtime = make_runtime()
    task = asyncio.create_task(runtime.init())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime.status.ready
    backend.connection.close.assert_awaited_once()
    await runtime.dispose()


async def test_resistant_probe_retains_owner_and_cannot_publish_late_ready(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    checker = AsyncMock()

    async def resistant(statement: Any) -> None:
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    backend.connection.execute.side_effect = resistant
    runtime = make_runtime(schema_check=checker, timeouts=replace(TEST_TIMEOUTS, shutdown_timeout_seconds=0.05))
    try:
        assert (await runtime.init()).reason == "timeout"
        assert (await runtime.probe()).reason == "starting"
        assert backend.connection.start.await_count == 1
        assert (await runtime.dispose()).reason == "close_incomplete"
        assert backend.driver.closed
        backend.engine.dispose.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(runtime._probe_task, 0.5)
        assert (await runtime.dispose()).state == "closed"
    checker.assert_not_awaited()
    backend.connection.scalar.assert_not_awaited()


async def test_close_error_preserves_primary_reason(backend: SimpleNamespace) -> None:
    backend.connection.scalar.side_effect = OSError("SECRET_DATABASE_URL")
    backend.connection.close.side_effect = RuntimeError("SECRET_DATABASE_URL")
    runtime = make_runtime()
    assert (await runtime.init()).reason == "connection_failed"
    assert backend.driver.closed
    assert (await runtime.probe()).reason == "close_incomplete"
    # fake close 失败没有触发 checkin，仍报告未释放，不能虚报 closed。
    assert (await runtime.dispose()).reason == "close_incomplete"
    backend.handlers["checkin"](backend.dbapi, backend.record)
    await runtime.dispose()


async def test_hanging_dispose_is_bounded_and_retained(backend: SimpleNamespace) -> None:
    release = asyncio.Event()
    backend.engine.dispose.side_effect = release.wait
    runtime = make_runtime(schema_check=AsyncMock(), timeouts=replace(TEST_TIMEOUTS, shutdown_timeout_seconds=0.03))
    await runtime.init()
    try:
        assert (await runtime.dispose()).reason == "close_incomplete"
        assert backend.driver.closed
        assert (await runtime.dispose()).reason == "close_incomplete"
        assert backend.engine.dispose.await_count == 1
    finally:
        release.set()
        await runtime._dispose_task
        assert (await runtime.dispose()).state == "closed"


async def test_swallowed_dispose_failure_is_not_reported_closed(backend: SimpleNamespace) -> None:
    backend.engine.dispose.side_effect = None
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    assert (await runtime.dispose()).reason == "close_incomplete"
    assert backend.driver.closed
    assert (await runtime.dispose()).state == "closed"


async def test_real_engine_connection_refused_is_not_ready() -> None:
    # 绑定但不 listen 的本地 socket 保留端口，避免依赖机器上的 PostgreSQL。
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        runtime = make_runtime(url=f"postgresql+asyncpg://localhost:{port}/runtime_test")
        try:
            assert (await runtime.init()).reason in {"connection_failed", "timeout"}
            assert not runtime.status.ready
            assert (await runtime.probe()).reason in {"connection_failed", "timeout"}
        finally:
            assert (await runtime.dispose()).state == "closed"


async def test_real_engine_logs_redact_echo_and_pool_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    runtime = make_runtime(echo=True)
    runtime._build_engine()
    try:
        with caplog.at_level(logging.DEBUG):
            for owner in (runtime._engine.sync_engine, runtime._engine.sync_engine.pool):
                try:
                    raise RuntimeError("SECRET_DATABASE_URL")
                except RuntimeError:
                    owner.logger.error("SECRET_SQL %s", "SECRET_ARGUMENT", exc_info=True)  # noqa: G201 — 模拟池原始日志。
        assert "SECRET" not in caplog.text
        assert "details redacted" in caplog.text
    finally:
        await runtime.dispose()


async def test_concurrent_dispose_failure_keeps_one_owner(backend: SimpleNamespace) -> None:
    backend.engine.dispose.side_effect = None
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    results = await asyncio.gather(runtime.dispose(), runtime.dispose())
    assert all(result.reason == "close_incomplete" for result in results)
    assert backend.engine.dispose.await_count == 1
    await runtime.dispose()


async def test_dispose_preserves_external_connection_during_initialization(backend: SimpleNamespace) -> None:
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    backend.handlers["connect"](backend.dbapi, object())
    assert (await runtime.dispose()).reason == "close_incomplete"
    assert not backend.driver.closed
    backend.handlers["checkin"](backend.dbapi, backend.record)
    await runtime.dispose()


async def test_factory_session_is_owned_before_first_driver_connection() -> None:
    runtime = make_runtime()
    runtime._build_engine()
    runtime._status = database_module.DatabaseStatus("ready", None)
    session = runtime.session_factory()
    try:
        assert (await runtime.dispose()).reason == "close_incomplete"
    finally:
        await session.close()
        assert (await runtime.dispose()).state == "closed"


@pytest.mark.parametrize("version", [None, 0, -1, "SECRET_VERSION", True])
async def test_invalid_schema_observation_cannot_open_factory(backend: SimpleNamespace, version: Any) -> None:
    backend.connection.scalar.return_value = version
    checker = AsyncMock()
    runtime = make_runtime(schema_check=checker)
    try:
        status = await runtime.init()
        assert status.reason == "schema_mismatch"
        assert status.schema_version is None
        checker.assert_not_awaited()
    finally:
        await runtime.dispose()


async def test_explicit_probe_recovers_without_rebuilding_engine(backend: SimpleNamespace) -> None:
    backend.connection.execute.side_effect = OSError("SECRET")
    runtime = make_runtime(schema_check=AsyncMock())
    try:
        assert (await runtime.init()).reason == "connection_failed"
        backend.connection.execute.side_effect = None
        assert (await runtime.probe()).ready
        assert len(backend.builds) == 1
    finally:
        await runtime.dispose()


async def test_close_budget_is_not_the_entire_probe_budget(backend: SimpleNamespace) -> None:
    release = asyncio.Event()
    backend.connection.close.side_effect = release.wait
    runtime = make_runtime(timeouts=replace(TEST_TIMEOUTS, probe_timeout_seconds=5, cleanup_timeout_seconds=0.03))
    started = asyncio.get_running_loop().time()
    try:
        assert (await runtime.init()).reason == "timeout"
        assert asyncio.get_running_loop().time() - started < 0.5
        assert backend.driver.closed
    finally:
        release.set()
        if runtime._probe_task is not None:
            await asyncio.gather(runtime._probe_task, return_exceptions=True)
        backend.handlers["checkin"](backend.dbapi, backend.record)
        await runtime.dispose()


async def test_concurrent_probe_does_not_queue_more_connection_work(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def check(connection: Any) -> None:
        entered.set()
        await release.wait()

    runtime = make_runtime(schema_check=check)
    initializing = asyncio.create_task(runtime.init())
    try:
        await entered.wait()
        assert (await runtime.probe()).reason == "starting"
        assert not await runtime.ping()
        assert backend.connection.start.await_count == 1
        release.set()
        assert (await initializing).ready
    finally:
        release.set()
        await initializing
        await runtime.dispose()


async def test_dispose_cancels_active_probe_without_late_readiness(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()

    async def check(connection: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    runtime = make_runtime(schema_check=check)
    initializing = asyncio.create_task(runtime.init())
    await entered.wait()
    assert (await runtime.dispose()).state == "closed"
    assert (await initializing).state in {"closing", "closed"}
    assert runtime.status.state == "closed"


async def test_cancelled_dispose_keeps_task_for_explicit_completion(backend: SimpleNamespace) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def close() -> None:
        entered.set()
        await release.wait()
        backend.driver.closed = True

    backend.engine.dispose.side_effect = close
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    closing = asyncio.create_task(runtime.dispose())
    try:
        await entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert runtime.status.reason == "close_incomplete"
        assert not runtime._dispose_task.done()
    finally:
        release.set()
        assert (await runtime.dispose()).state == "closed"
    assert backend.engine.dispose.await_count == 1


async def test_expired_parent_deadline_does_not_start_new_disposal(backend: SimpleNamespace) -> None:
    runtime = make_runtime()
    await runtime.init()
    assert (await runtime.dispose(deadline=asyncio.get_running_loop().time())).reason == "close_incomplete"
    backend.engine.dispose.assert_not_awaited()
    assert (await runtime.dispose()).state == "closed"


async def test_dispose_before_init_never_creates_an_engine(backend: SimpleNamespace) -> None:
    runtime = make_runtime()
    assert (await runtime.dispose()).state == "closed"
    assert (await runtime.init()).state == "closed"
    assert not backend.builds


async def test_missing_driver_is_a_safe_unavailable_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(**kwargs: Any) -> None:
        raise ModuleNotFoundError("SECRET_URL")

    monkeypatch.setattr(database_module, "create_async_engine", missing)
    runtime = make_runtime()
    assert (await runtime.init()).reason == "driver_missing"
    assert (await runtime.dispose()).state == "closed"


async def test_timed_out_ping_with_live_owner_blocks_new_session(backend: SimpleNamespace) -> None:
    runtime = make_runtime(schema_check=AsyncMock())
    await runtime.init()
    factory = runtime.session_factory
    release = asyncio.Event()

    async def resistant(statement: Any) -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    backend.connection.execute.side_effect = resistant
    try:
        assert not await runtime.ping()
        with pytest.raises(database_module.DatabaseRuntimeError, match="close_incomplete"):
            factory()
        with pytest.raises(database_module.DatabaseRuntimeError, match="close_incomplete"):
            backend.handlers["checkout"](backend.dbapi, object(), None)
    finally:
        release.set()
        await runtime._probe_task
        await runtime.dispose()


async def test_cleanup_timeout_does_not_replace_authentication_failure(backend: SimpleNamespace) -> None:
    error = RuntimeError("SECRET")
    error.sqlstate = "28P01"
    backend.connection.scalar.side_effect = error
    backend.connection.close.side_effect = asyncio.Event().wait
    runtime = make_runtime(timeouts=replace(TEST_TIMEOUTS, cleanup_timeout_seconds=0.02))
    try:
        assert (await runtime.init()).reason == "authentication_failed"
    finally:
        if runtime._probe_task is not None:
            await asyncio.gather(runtime._probe_task, return_exceptions=True)
        backend.handlers["checkin"](backend.dbapi, backend.record)
        await runtime.dispose()


def test_runtime_starts_with_factory_closed() -> None:
    """未执行真实探测前不得拿到可用会话工厂。"""
    runtime = database_module.DatabaseRuntime(
        url="postgresql+asyncpg://localhost/runtime_test",
        pool_size=1,
        max_overflow=0,
        echo=False,
        timeouts=TEST_TIMEOUTS,
    )
    assert runtime.status.state == "new"
    with pytest.raises(database_module.DatabaseRuntimeError, match="starting"):
        _ = runtime.session_factory


def test_asyncpg_driver_is_importable() -> None:
    """当前 Python 环境必须能真实导入正式 PostgreSQL 驱动。"""
    driver = import_module("asyncpg")
    assert callable(driver.connect)


async def test_postgresql_async_engine_can_load_driver() -> None:
    """SQLAlchemy 能加载 asyncpg 方言；构造成功不作为数据库就绪证据。"""
    engine = create_async_engine("postgresql+asyncpg://localhost/database_dependency_test")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "asyncpg"
    finally:
        await engine.dispose()
