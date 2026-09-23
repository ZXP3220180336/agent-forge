"""共享 PostgreSQL 资源 Owner；只读探测，不执行迁移或业务重试。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class DatabaseRuntimeError(RuntimeError):
    """仅携带稳定原因码，不保留驱动异常文本。"""


@dataclass(frozen=True, kw_only=True)
class DatabaseTimeouts:
    """运行时秒数配置；默认值和有限正数校验统一由 Settings 提供。"""

    connect_timeout_seconds: float
    pool_timeout_seconds: float
    operation_timeout_seconds: float
    probe_timeout_seconds: float
    cleanup_timeout_seconds: float
    shutdown_timeout_seconds: float


@dataclass(frozen=True)
class DatabaseStatus:
    """当前准入快照；版本号只是观察值，不能证明历史完整性。"""

    state: str = "new"
    reason: str | None = "starting"
    schema_version: int | None = None

    @property
    def ready(self) -> bool:
        """当前完整探测是否允许新业务准入。"""
        return self.state == "ready"


class _SafeDatabaseLog(logging.Filter):
    """池内部会记录被吞掉的关闭异常；echo 也必须经过同一脱敏出口。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """保留事件级别，移除 SQL、凭证与异常链。"""
        record.msg = "database event (details redacted)"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


class _RuntimeSession(AsyncSession):
    """从创建到 close 完成均登记 Owner，覆盖驱动建连前及 pre_ping 阶段。"""

    def __init__(self, *, runtime: DatabaseRuntime, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._runtime = runtime

    async def close(self) -> None:
        """关闭失败/取消时仍保留 Owner，禁止数据库抢先释放。"""
        await super().close()
        self._runtime._sessions.discard(self)


# 可原样上报的稳定原因码：前三个来自受信检查器的结论，后两个由本运行时的准入与清理路径抛出。
# 集合外的 DatabaseRuntimeError 文本一律按未知缺陷处理，避免回显任意异常文本。
_TRUSTED_REASON_CODES = frozenset(
    {"schema_missing", "schema_mismatch", "permission_denied", "closing", "close_incomplete"}
)


def _reason(error: Exception) -> str:
    if isinstance(error, DatabaseRuntimeError):
        code = str(error)
        return code if code in _TRUSTED_REASON_CODES else "internal_error"
    if isinstance(error, (TimeoutError, PoolTimeoutError)):
        return "timeout"
    if isinstance(error, ModuleNotFoundError):
        return "driver_missing"
    original = getattr(error, "orig", error)
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate in {"28000", "28P01"}:
        return "authentication_failed"
    if sqlstate in {"42501", "25006"}:
        return "permission_denied"
    if sqlstate == "42P01":
        return "schema_missing"
    if sqlstate == "42703":
        return "schema_mismatch"
    if sqlstate == "57014":
        return "timeout"
    if isinstance(original, (OSError, ConnectionError)) or (sqlstate and sqlstate.startswith("08")):
        return "connection_failed"
    return "internal_error"


class DatabaseRuntime:
    """持有一个 engine、工厂与探测/关闭任务，每次等待共享绝对 deadline。

    schema_check 是后续迁移模块提供的只读全量校验，缺失时 fail closed。
    使用方必须先排空业务事务再 dispose；运行时不终止借出的业务连接。
    """

    def __init__(
        self,
        *,
        url: str,
        pool_size: int,
        max_overflow: int,
        echo: bool,
        timeouts: DatabaseTimeouts,
        schema_check: Callable[[AsyncConnection], Awaitable[None]] | None = None,
    ) -> None:
        # 数值/URL 校验由 Settings 负责；此对象仅消费已校验的配置。
        # 整组参数展开给 create_async_engine，键值类型天然异构；显式标注 Any，避免检查器把展开值绑定到首个位置参数 url。
        self._engine_options: dict[str, Any] = {
            "url": url,
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "echo": echo,
            "pool_timeout": timeouts.pool_timeout_seconds,
            "pool_pre_ping": True,
            "hide_parameters": True,
            "connect_args": {
                "timeout": timeouts.connect_timeout_seconds,
                "command_timeout": timeouts.operation_timeout_seconds,
            },
        }
        self._timeouts = timeouts
        self._schema_check = schema_check
        self._engine: AsyncEngine | None = None
        self._maker: async_sessionmaker[_RuntimeSession] | None = None

        # 准入状态快照：new / ready / unavailable / closing / closed。
        self._status = DatabaseStatus()
        # 至多一个探测任务；未运行时为 None。
        self._probe_task: asyncio.Task | None = None
        self._dispose_task: asyncio.Task | None = None
        self._probe_stopped = False
        self._probe_failure: str | None = None
        self._cleanup_started = asyncio.Event()
        self._cleanup_deadline = 0.0
        self._cleanup_failed = False

        # 驱动在 connect 事件中登记，早于方言初始化 SQL；借出记录独立跟踪。
        # key 是底层驱动连接，value 是当前占用的异步任务（None = 空闲）
        self._drivers: dict[Any, asyncio.Task | None] = {}
        # key 是连接池记录，value 是借走未归还的任务
        self._borrowed: dict[Any, asyncio.Task | None] = {}
        # 已创建未关闭的 Session
        self._sessions: set[AsyncSession] = set()

    @property
    def status(self) -> DatabaseStatus:
        """返回不可变快照，不包含连接配置。"""
        return self._status

    @property
    def session_factory(self) -> Callable[[], AsyncSession]:
        """基础设施适配器入口；缓存下来的工厂也会重新检查准入。"""
        self._require_ready()
        return self._new_session

    def _require_ready(self) -> None:
        if self._cleanup_failed or (self._probe_stopped and self._probe_task is not None):
            raise DatabaseRuntimeError("close_incomplete")
        if not self._status.ready:
            raise DatabaseRuntimeError(self._status.reason or "closing")

    def _new_session(self) -> AsyncSession:
        self._require_ready()
        assert self._maker is not None  # 准入依赖引擎已建立，工厂与引擎同一处赋值
        session = self._maker()
        self._sessions.add(session)
        return session

    async def init(self) -> DatabaseStatus:
        """创建唯一引擎并执行完整探测，不因引擎构造成功就开放能力。"""
        if self._status.state in {"closing", "closed"}:
            return self._status
        if self._engine is None:
            try:
                self._build_engine()
            except Exception as error:  # noqa: BLE001 — 外部驱动边界脱敏，未知错误仍抛内部错误。
                reason = _reason(error)
                self._status = DatabaseStatus("unavailable", reason)
                if reason == "internal_error":
                    raise DatabaseRuntimeError(reason) from None
                return self._status
        return await self.probe()

    def _build_engine(self) -> None:
        """构造唯一引擎，并挂上实例级脱敏日志、池事件登记与 Session 工厂。"""
        # 实例唯一名：脱敏过滤器只挂到本实例的 engine / pool logger，不影响其他引擎。
        name = f"runtime_{id(self):x}"

        # 构造引擎（先落在局部变量：引擎、监听器与工厂全部就绪才发布，半构建不得被当成可用引擎）
        engine = create_async_engine(**self._engine_options, logging_name=name, pool_logging_name=name)
        # 池会吞掉关闭异常并自行记日志，echo 也走同一出口，两者都必须在写入日志前脱敏。
        # 目标 logger 从实例取：池实现与 echo 开关都会改变实例 logger 的类型，不能拼类名。
        for target in (engine.sync_engine.logger, engine.sync_engine.pool.logger):
            inner = target if isinstance(target, logging.Logger) else target.logger
            inner.addFilter(_SafeDatabaseLog())

        # 监听「连接创建」事件
        # insert=True 排到其他监听器之前：方言初始化 SQL 尚未执行就已登记驱动 Owner，
        # 使初始化失败也留有可核验记录；该监听器还会在关闭期终止迟到的连接。
        event.listen(engine.sync_engine, "connect", self._connect, insert=True)
        # 监听「连接从池子里被借出」事件
        event.listen(engine.sync_engine, "checkout", self._checkout)
        # 监听「连接用完归还到池子」事件
        event.listen(engine.sync_engine, "checkin", self._checkin)

        # 工厂只产出 _RuntimeSession：创建即登记 Owner，close 成功才解除；
        # expire_on_commit=False 避免关闭阶段触发懒加载 I/O，close_resets_only=False 使 close 后不可复用。
        maker = async_sessionmaker(
            engine, class_=_RuntimeSession, runtime=self, expire_on_commit=False, close_resets_only=False
        )
        # 无 await 的连续赋值：engine 非空即等价于构建完成，_new_session 的断言依赖这一点。
        self._engine = engine
        self._maker = maker

    def _connect(self, dbapi_connection: Any, record: Any) -> None:
        """登记新驱动归属，并在关闭阶段终止迟到的连接。

        Raises:
            DatabaseRuntimeError: 状态已进入 closing / closed。
        """
        # 顺带回收已关闭驱动，避免 _drivers 随重连次数无界增长。
        self._drivers = {driver: owner for driver, owner in self._drivers.items() if not driver.is_closed()}
        driver = dbapi_connection.driver_connection
        self._drivers[driver] = asyncio.current_task()
        # 关闭已开始：这条连接不留作可用驱动，也不让它继续初始化。
        if self._status.state in {"closing", "closed"}:
            driver.terminate()
            raise DatabaseRuntimeError("closing")

    def _checkout(self, dbapi_connection: Any, record: Any, proxy: Any) -> None:
        """复查准入后登记借出记录与驱动 Owner。

        Raises:
            DatabaseRuntimeError: 状态未就绪，或清理失败与探针已停止。
        """
        owner = asyncio.current_task()
        # 仅探针任务自身、且探针未被停止时跳过复查：首次探测时状态尚非 ready，没有豁免就建不了连接。
        # 探针被停止后连它自己也要复查，避免取消后的探针继续建立新连接。
        if owner is not self._probe_task or self._probe_stopped:
            try:
                self._require_ready()
            except DatabaseRuntimeError:
                dbapi_connection.driver_connection.terminate()
                raise
        self._borrowed[record] = owner
        self._drivers[dbapi_connection.driver_connection] = owner

    def _checkin(self, dbapi_connection: Any, record: Any) -> None:
        """销借出账；驱动 Owner 置 None 表示回到池中空闲。"""
        self._borrowed.pop(record, None)
        # 不删除条目：dispose 要保留每个已登记驱动来核验物理关闭状态。
        if dbapi_connection is not None:
            self._drivers[dbapi_connection.driver_connection] = None

    async def ping(self) -> bool:
        """只检查连接与 SELECT 1；不更新准入状态。"""
        result = await self._run_probe(full=False)
        return result is not None and result[0] is None

    async def probe(self) -> DatabaseStatus:
        """只读检查；未知程序缺陷抛出脱敏内部错误，不冒充可恢复故障。"""
        result = await self._run_probe(full=True)
        if result is not None and self._status.state not in {"closing", "closed"}:
            reason, version = result
            self._status = DatabaseStatus("unavailable" if reason else "ready", reason, version)
        return self._status

    async def _run_probe(self, *, full: bool) -> tuple[str | None, int | None] | None:
        """
        探测入口,管**调度、控并发、卡超时、收结果、做清理**
        - 输入：`full=True` 全量探测（连通性 + 表结构版本校验）；`full=False` 快速探测（只测连通）
        - 输出：`(失败原因, 数据库版本号)`；成功则原因是 `None`；无法启动则返回 `None`
        """

        # 引擎缺失，或状态已是 closing / closed → 不启动工作任务
        if self._engine is None or self._status.state in {"closing", "closed"}:
            return None

        # 并发控制：保证永远只有一个探测任务在运行。
        # 上一个没跑完就直接返回状态，绝不排队、不重复创建，防止探测任务堆积挤爆数据库。
        if self._probe_task is not None:
            # 不排队或生成后台重连；迟到的旧探针必须先完成收尾。
            if not self._probe_task.done():
                return "starting", None  # 还在跑，返回「正在启动」，不重复开
            if not self._probe_task.cancelled():
                self._probe_task.result()  # 已经跑完，收掉结果
            self._probe_task = None

        # 故障熔断：清理失败就不再接入
        # 如果上一次探测的清理过程失败了（连接没关干净），直接熔断，不再启动新探测，避免资源越漏越多。
        if self._cleanup_failed:
            return "close_incomplete", None

        # 超时预算：把清理时间提前留出来
        loop = asyncio.get_running_loop()
        # 探测总预算
        deadline = loop.time() + self._timeouts.probe_timeout_seconds
        # 总预算扣除清理预算后剩下的工作预算
        work_deadline = deadline - min(self._timeouts.cleanup_timeout_seconds, self._timeouts.probe_timeout_seconds / 2)

        self._probe_stopped = False
        self._probe_failure = None
        self._cleanup_started.clear()

        # 创建唯一探测任务
        task = self._probe_task = asyncio.create_task(self._probe(full, work_deadline))
        # 创建清理开始信号
        cleanup_signal = asyncio.create_task(self._cleanup_started.wait())
        try:
            # 双任务等待：探测完成 / 清理开始，哪个先到都响应
            await asyncio.wait(
                {task, cleanup_signal},
                timeout=max(0, work_deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 如果清理先触发了，会再给探测任务留一点收尾时间。
            if cleanup_signal.done() and not task.done():
                await self._wait(task, min(deadline, self._cleanup_deadline))

            if not task.done():
                # 探测没做完 → 判定超时，记录失败原因，执行停止流程
                result = (self._probe_failure or "timeout"), None
                await self._stop_probe(deadline)
            elif task.cancelled():
                # 探测被取消 → 返回「正在关闭」
                return "closing", None
            else:
                # 正常完成 → 返回探测结果（失败原因 + 版本号）
                result = task.result()

            # 只有完整探测才改写准入：内部缺陷对 probe()/init() 抛脱敏错误，对 ping() 只报告本次未通过。
            if full and result[0] == "internal_error":
                self._status = DatabaseStatus("unavailable", "internal_error")
                raise DatabaseRuntimeError("internal_error")

            return result
        except asyncio.CancelledError:
            if full and self._status.state not in {"closing", "closed"}:
                self._status = DatabaseStatus("unavailable", "starting")
            await self._stop_probe(deadline)
            raise
        finally:
            # 强制取消清理信号、回收任务、清空探测任务标记，保证不管成功失败，任务状态都能复位。
            cleanup_signal.cancel()
            # Event.wait 无外部 I/O，不会吞取消；取走取消结果避免悬空等待者。
            await asyncio.gather(cleanup_signal, return_exceptions=True)
            if task.done() and self._probe_task is task:
                self._probe_task = None

    async def _probe(self, full: bool, deadline: float) -> tuple[str | None, int | None]:
        """
        实际执行探测：一步步建连接、测连通、查版本、校验结构，每一步之前都卡超时。

        """
        connection = None
        started = False
        reason = None
        version = None
        try:
            self._guard_probe(deadline)
            assert self._engine is not None  # _run_probe 已确认引擎存在才创建探测任务
            connection = self._engine.connect()
            await connection.start()
            started = True
            self._guard_probe(deadline)
            await connection.begin()
            self._guard_probe(deadline)
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            self._guard_probe(deadline)
            await connection.execute(text("SELECT 1"))
            if full:
                self._guard_probe(deadline)
                version = await connection.scalar(text("SELECT max(version) FROM public.schema_versions"))
                self._guard_probe(deadline)
                if type(version) is not int or version < 1:
                    version = None
                    reason = "schema_mismatch"
                elif self._schema_check is None:
                    reason = "schema_mismatch"
                else:
                    await self._schema_check(connection)
                    self._guard_probe(deadline)
        except asyncio.CancelledError:
            # 取消异常直接透传：任务被取消就直接往上抛，不做多余处理
            raise
        except Exception as error:  # noqa: BLE001 — 不让任务的底层异常链进入日志；未知错误由调用方抛出。
            # 其他异常全部脱敏：捕获所有异常，只提取错误原因码，不把底层原始异常栈抛出去，避免敏感信息泄露
            reason = _reason(error)
            self._probe_failure = reason
        finally:
            # 记录清理截止时间，触发「清理开始」信号
            self._cleanup_deadline = asyncio.get_running_loop().time() + self._timeouts.cleanup_timeout_seconds
            self._cleanup_started.set()
            if not started:
                # 连接根本没启动成功 → 直接强制终止所有探测相关连接
                self._terminate_probe_drivers()
            elif connection is not None:
                try:
                    # 不用 AsyncConnection.__aexit__：它创建 shield 后台 close 任务。
                    await connection.close()
                except asyncio.CancelledError:
                    self._cleanup_failed = True
                    self._terminate_probe_drivers()
                    raise
                except Exception:  # noqa: BLE001 — 清理失败不能覆盖主错误，保留 Owner 并关闭准入。
                    self._cleanup_failed = True
                    self._terminate_probe_drivers()
        return reason or ("close_incomplete" if self._cleanup_failed else None), version

    def _guard_probe(self, deadline: float) -> None:
        """
        探测护卫：探测已停止或超时则抛出超时异常
        """
        if self._probe_stopped or asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError

    def _terminate_probe_drivers(self) -> None:
        for driver, owner in self._drivers.items():
            if owner is self._probe_task and not driver.is_closed():
                try:
                    # asyncpg 原生 terminate 是同步 abort；适配器 terminate 可能等待。
                    driver.terminate()
                except Exception:  # noqa: BLE001 — 保留清理失败事实，不回显驱动异常。
                    self._cleanup_failed = True

    async def _wait(self, task: asyncio.Task, deadline: float) -> bool:
        done, _ = await asyncio.wait({task}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        return bool(done)

    async def _stop_probe(self, deadline: float) -> bool:
        task = self._probe_task
        if task is None:
            return True
        self._probe_stopped = True
        if not task.done():
            task.cancel()
            try:
                cleanup_deadline = min(
                    deadline, asyncio.get_running_loop().time() + self._timeouts.cleanup_timeout_seconds
                )
                if self._cleanup_started.is_set():
                    cleanup_deadline = min(cleanup_deadline, self._cleanup_deadline)
                await self._wait(task, cleanup_deadline)
            finally:
                self._terminate_probe_drivers()
        if task.done():
            if not task.cancelled():
                task.result()  # worker 只返回脱敏结果；取走完成结果。
            if self._probe_task is task:
                self._probe_task = None
            return True
        return False

    async def dispose(self, *, deadline: float | None = None) -> DatabaseStatus:
        """关闭准入，有界等待自己的任务；未排空则保留资源并报告未完成。"""
        if self._status.state == "closed":
            return self._status
        self._status = DatabaseStatus("closing", "closing")
        own_deadline = asyncio.get_running_loop().time() + self._timeouts.shutdown_timeout_seconds
        deadline = own_deadline if deadline is None else min(deadline, own_deadline)
        try:
            if (
                not await self._stop_probe(deadline)
                or self._sessions
                or self._borrowed
                or any(owner is not None and not driver.is_closed() for driver, owner in self._drivers.items())
            ):
                self._status = DatabaseStatus("closing", "close_incomplete")
                return self._status

            if self._engine is None:
                self._status = DatabaseStatus("closed", None)
                return self._status

            if asyncio.get_running_loop().time() >= deadline:
                self._status = DatabaseStatus("closing", "close_incomplete")
                return self._status

            if self._dispose_task is None:
                self._dispose_task = asyncio.create_task(self._dispose_engine())
            task = self._dispose_task
            complete = await self._wait(task, deadline)
            if complete and not task.cancelled() and task.result():
                self._status = DatabaseStatus("closed", None)
            else:
                self._terminate_idle_drivers()
                self._status = DatabaseStatus("closing", "close_incomplete")
                if complete and self._dispose_task is task:
                    # 仅显式再次 dispose 可重试清理；业务与探针不再启动。
                    self._dispose_task = None
            return self._status
        except asyncio.CancelledError:
            self._status = DatabaseStatus("closing", "close_incomplete")
            raise

    async def _dispose_engine(self) -> bool:
        try:
            assert self._engine is not None  # dispose() 已确认引擎存在才创建关闭任务
            await self._engine.dispose()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 关闭失败统一报告 close_incomplete，保留引擎。
            return False
        return all(driver.is_closed() for driver in self._drivers)

    def _terminate_idle_drivers(self) -> None:
        """仅终止空闲驱动；仍被业务或探针持有的驱动留给其 Owner 收尾，不得强关在用事务。"""
        for driver, owner in self._drivers.items():
            if owner is None and not driver.is_closed():
                try:
                    driver.terminate()
                except Exception:  # noqa: BLE001 — 物理状态仍由 is_closed 验证，不把失败当成功。
                    self._cleanup_failed = True
