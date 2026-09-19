"""真实工具任务的进程内 Owner；取消等待不等于底层执行已经结束。"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Any

from app.domain.ports.tool_execution import ToolCallContext
from app.integration.tools.admission import ToolPermit
from app.shared.exceptions import ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError


class ToolEffectClass(StrEnum):
    """由可信适配器声明的操作效果类别。"""

    READ_ONLY = "READ_ONLY"  # 确认只读
    MAY_WRITE = "MAY_WRITE"  # 可能产生副作用
    UNKNOWN = "UNKNOWN"  # 无法确认


@dataclass(frozen=True)
class ToolExecutionSpec:
    """可信适配器声明；UNKNOWN 不能推导为安全读操作。"""

    effect_class: ToolEffectClass = ToolEffectClass.UNKNOWN  # 调用前的能力声明: 预期效果类别
    audit_required: bool = False  # 声明该操作是否需要强制审计
    resource_claims: tuple[object, ...] = ()  # 声明操作会占用或影响哪些资源
    tool_version: str = ""  # 标识产生这份声明的工具版本


def read_execution_spec(
    describe: Callable[[dict[str, Any]], ToolExecutionSpec],
    parameters: dict[str, Any],
) -> ToolExecutionSpec:
    """隔离适配器声明故障，保守回落 UNKNOWN；生命周期控制信号保持传播。"""
    try:
        spec = describe(parameters)
    except ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError:
        raise
    except Exception:  # noqa: BLE001 — 外部适配器边界，不能推定执行安全
        return ToolExecutionSpec()
    return spec if isinstance(spec, ToolExecutionSpec) else ToolExecutionSpec()


def is_execution_enabled(spec: ToolExecutionSpec) -> bool:
    """持久保护完成前，仅启用可信只读且不要求强制审计的操作。"""
    return spec.effect_class is ToolEffectClass.READ_ONLY and spec.audit_required is False


@dataclass(frozen=True)
class ToolExecutionSettings:
    """宿主共享的有限执行与收尾配置，不保存单次调用状态。"""

    cleanup_timeout_seconds: float = 1.0  # 一次真实工具调用被取消或超时后，最多等待多久让它自行清理。
    observation_timeout_seconds: float = 0.2  # 一次调用中审计、Hook 等非关键观测共用的时间预算。
    max_recovery_records: int = 30  # Supervisor 最多同时持有多少条执行跟踪记录。
    shutdown_timeout_seconds: float = 5.0  # 关闭工具服务时，等待刷新、在途工具和相关收尾工作的时间上限。

    def __post_init__(self) -> None:
        for name in ("cleanup_timeout_seconds", "observation_timeout_seconds", "shutdown_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为有限正数")
        if type(self.max_recovery_records) is not int or self.max_recovery_records <= 0:
            raise ValueError("max_recovery_records 必须为正整数")


class ToolShutdownIncompleteError(RuntimeError):
    """真实工具尚未退出；调用方不得关闭它仍在使用的依赖。"""


class ToolAttemptTimeoutError(TimeoutError):
    """单次工具等待预算耗尽，与工具内部自然抛出的 TimeoutError 分开。"""


def check_abort(call: ToolCallContext) -> None:
    """同一检查点按取消、总期限、运行停止的顺序裁决。"""
    kwargs = {"run_id": call.run_id, "operation_id": call.operation_id}
    if any(event.is_set() for event in call.cancel_events):
        raise ToolCancelledError(**kwargs)
    if call.deadline is not None and time.monotonic() >= call.deadline:
        raise ToolDeadlineExceededError(**kwargs)
    if call.run_stop.is_set():
        raise ToolRunStoppedError(**kwargs)


async def wait_for_abort(call: ToolCallContext) -> None:
    """在一段正在等待的操作旁边，持续监听这次工具调用是否应该终止。"""

    # 进入等待前先检查一次。若信号已经发生，立即抛出对应异常，不必创建等待任务。
    check_abort(call)

    # 这些任务只是在等信号，不执行工具业务。
    tasks = [asyncio.create_task(event.wait()) for event in (*call.cancel_events, call.run_stop)]
    try:
        # 根据 call.deadline 计算还剩多少时间
        remaining = None if call.deadline is None else max(0.0, call.deadline - time.monotonic())
        # 任一事件先置位，或期限先到，就结束等待。
        await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)

        # 上方结束等待说明“有信号了”，把它转换成明确的 ToolCancelledError、ToolDeadlineExceededError
        # 或 ToolRunStoppedError。
        check_abort(call)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ToolAttemptHandle:
    """持有实际调用句柄；业务 Permit 由 Supervisor 归还，观测任务不得派线程。"""

    def __init__(
        self,
        supervisor: ToolExecutionSupervisor,
        call: ToolCallContext,
        permit: ToolPermit | None,
        owner: object,
        on_complete: Callable[[ToolAttemptHandle], None] | None,
        on_transfer: Callable[[ToolAttemptHandle], None] | None,
        on_release: Callable[[], None] | None,
    ) -> None:
        """登记调用所有权；真实启动及回调交付统一由 Supervisor 管理。"""
        self.call = call
        self.owner = owner
        self.value: Any = None
        self.error: BaseException | None = None
        self.completed = False
        self.transferred = False
        self._supervisor = supervisor
        self._permit = permit
        self._on_complete = on_complete
        self._on_transfer = on_transfer
        self._on_release = on_release
        self._threads: set[Future[Any]] = set()
        self.thread_results: list[Any] = []
        self.thread_errors: list[BaseException] = []
        self._done = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None
        self._callback_error: BaseException | None = None
        self._stopping = False
        self._unacknowledged = False

    @property
    def callback_error(self) -> BaseException | None:
        """已留存的事实交付或释放回调编程错误。"""
        return self._callback_error

    def retain_record(self) -> None:
        """完成回调中标记尚无人确认接管的结果，继续占用有界事实跟踪容量。"""
        self._unacknowledged = True

    def check_abort(self) -> None:
        """检查运行控制与句柄寿命，禁止终止后继续发起底层工作。"""
        check_abort(self.call)
        if self.completed or (self._task is not None and self._task.done()):
            # 已归还或正在归还 Permit 的句柄不能被适配器后台协程继续复用。
            raise RuntimeError("工具 attempt 已经结束，不能启动新工作")
        if self._stopping:
            raise asyncio.CancelledError()

    async def run_sync(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """宿主线程池保留 concurrent Future，asyncio 取消只停止等待。"""
        self.check_abort()
        if self._permit is None:
            raise RuntimeError("无业务 Permit 的观测任务不能派发线程工作")
        if self._threads:
            raise RuntimeError("同一 attempt 不得并行提交多个线程工作")
        loop = asyncio.get_running_loop()
        future = self._supervisor._pool.submit(partial(fn, *args, **kwargs))
        self._threads.add(future)
        future.add_done_callback(lambda done: loop.call_soon_threadsafe(self._thread_done, done))
        bridge = asyncio.wrap_future(future)
        bridge.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.shield(bridge)
        except asyncio.CancelledError:
            # 等待取消后，线程值/异常未交给适配器解释。即使线程在清理窗口内
            # 完成，也必须保留其记录，不能用协程的失败事实代替线程证据。
            self.retain_record()
            raise

    def _thread_done(self, future: Future[Any]) -> None:
        self._threads.discard(future)
        # 收取异常，避免只取消 asyncio 包装层后遗留未观察的真实失败。
        try:
            self.thread_results.append(future.result())
        except BaseException as error:  # noqa: BLE001 -- Owner 必须先保存包括硬取消在内的真实结局
            self.thread_errors.append(error)
        self._supervisor._finish(self)


class ToolExecutionSupervisor:
    """先预留接管条目再启动任务；真实结束才释放容量和依赖引用。"""

    def __init__(self, settings: ToolExecutionSettings | None = None, *, max_workers: int = 3) -> None:
        """建立有界跟踪集合及宿主线程池，工作线程数由共享准入容量装配。"""
        self.settings = settings or ToolExecutionSettings()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tool")
        self._records: set[ToolAttemptHandle] = set()
        self._closed = False

    @property
    def records(self) -> tuple[ToolAttemptHandle, ...]:
        """有界 Owner 快照；转移记录保留到后续恢复责任显式接管。"""
        return tuple(self._records)

    def owns(self, owner: object) -> bool:
        """判断某工具实例是否仍被真实在途工作使用，供卸载前检查。"""
        return any(record.owner is owner and not record.completed for record in self._records)

    def start(
        self,
        factory: Callable[[ToolAttemptHandle], Awaitable[Any]],
        *,
        call: ToolCallContext,
        permit: ToolPermit | None,
        owner: object = None,
        on_complete: Callable[[ToolAttemptHandle], None] | None = None,
        on_transfer: Callable[[ToolAttemptHandle], None] | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> ToolAttemptHandle | None:
        """预占跟踪容量；permit=None 仅供非业务观测，不能派发线程工作。"""
        check_abort(call)
        if self._closed or len(self._records) >= self.settings.max_recovery_records:
            return None
        handle = ToolAttemptHandle(self, call, permit, owner, on_complete, on_transfer, on_release)
        self._records.add(handle)

        async def invoke() -> Any:
            handle.check_abort()
            return await factory(handle)

        handle._task = asyncio.create_task(invoke())
        handle._task.add_done_callback(lambda _: self._finish(handle))
        return handle

    def _finish(self, handle: ToolAttemptHandle) -> None:
        task = handle._task
        if handle.completed or task is None or not task.done():
            return
        try:
            handle.value = task.result()
        except BaseException as error:  # noqa: BLE001 -- Owner 必须先保存包括硬取消在内的真实结局
            handle.error = error
        if handle._threads:
            return
        handle.completed = True
        try:
            if handle._on_complete is not None:
                handle._on_complete(handle)
        except BaseException as error:  # noqa: BLE001 -- Owner 必须先保存包括硬取消在内的真实结局
            handle._callback_error = error
            handle.call.run_stop.set()
        finally:
            handle._on_complete = handle._on_transfer = None
            try:
                if handle._on_release is not None:
                    handle._on_release()
            except BaseException as error:  # noqa: BLE001 -- Owner 必须先保存包括硬取消在内的真实结局
                handle._callback_error = error
                handle.call.run_stop.set()
            finally:
                handle._on_release = None
                if handle._permit is not None:
                    handle._permit.release()
                handle._done.set()
                if not handle.transferred and not handle._unacknowledged and handle._callback_error is None:
                    self._records.discard(handle)

    def _transfer(self, handle: ToolAttemptHandle) -> None:
        if handle.transferred or handle.completed:
            return
        handle.transferred = True
        try:
            if handle._on_transfer is not None:
                handle._on_transfer(handle)
        except BaseException as error:
            handle._callback_error = error
            handle.call.run_stop.set()
            raise
        finally:
            # 后台句柄不得透过 callback 保留 Domain collector/Agent。
            handle._on_complete = handle._on_transfer = None

    async def _stop(self, handle: ToolAttemptHandle) -> None:
        handle._stopping = True
        if handle._task is not None and not handle._task.done():
            handle._task.cancel()
        deadline = time.monotonic() + self.settings.cleanup_timeout_seconds
        if handle.call.cleanup_deadline is not None:
            deadline = min(deadline, handle.call.cleanup_deadline)
        waiter = asyncio.create_task(handle._done.wait())
        try:
            await asyncio.wait({waiter}, timeout=max(0.0, deadline - time.monotonic()))
        finally:
            waiter.cancel()
            # 无 await 的所有权转移也覆盖清理期间的第二次硬取消。
            self._finish(handle)
            self._transfer(handle)

    async def wait(self, handle: ToolAttemptHandle, *, timeout: float | None = None) -> Any:
        """接管结果后判控制；停止等待前完成有界清理或移交，硬取消保持原类型。"""
        done = asyncio.create_task(handle._done.wait())
        abort = asyncio.create_task(wait_for_abort(handle.call))
        try:
            ready, _ = await asyncio.wait({done, abort}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            # 结果先接管（完成 callback），然后再裁决控制信号。
            self._finish(handle)
            if abort in ready:
                await abort
            check_abort(handle.call)
            if not handle.completed:
                raise ToolAttemptTimeoutError("工具单次执行超时")
            if handle._callback_error is not None:
                raise handle._callback_error
            if handle.error is not None:
                raise handle.error
            return handle.value
        except asyncio.CancelledError:
            try:
                await self._stop(handle)
            except Exception as error:  # noqa: BLE001 -- 硬取消保留，收尾异常留存 Owner
                # 清理/事实失败已经留在 Owner；不得把宿主硬取消改成业务失败。
                handle._callback_error = error
            raise
        except BaseException:
            await self._stop(handle)
            if handle._callback_error is not None:
                raise handle._callback_error
            raise
        finally:
            done.cancel()
            abort.cancel()
            await asyncio.gather(done, abort, return_exceptions=True)

    async def close(self) -> None:
        """有限等待真实退出；失败时保留 Owner，阻止上层卸载依赖。"""
        self._closed = True
        active = [handle for handle in self._records if not handle.completed]
        for handle in active:
            handle._stopping = True
            if handle._task is not None:
                handle._task.cancel()
        waiters = [asyncio.create_task(handle._done.wait()) for handle in active]
        try:
            if waiters:
                await asyncio.wait(waiters, timeout=self.settings.shutdown_timeout_seconds)
        finally:
            for waiter in waiters:
                waiter.cancel()
            for handle in active:
                self._finish(handle)
                self._transfer(handle)
        if any(not handle.completed for handle in self._records):
            raise ToolShutdownIncompleteError("工具执行未排空，必须保留其依赖")
        self._pool.shutdown(wait=False, cancel_futures=True)
