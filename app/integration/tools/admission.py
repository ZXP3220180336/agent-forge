"""工具调用的共享准入与有界排队。

准入只拥有容量、等待队列和 Permit 生命周期，不执行工具、不记录事实，也不决定
领域终态。所有状态都在所属事件循环内同步修改，避免把 asyncio 原语交给工作线程。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

from app.domain.ports.tool_execution import ToolCallContext
from app.shared.exceptions import (
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)


@dataclass
class _Waiter:
    """一个正在等候准入的调用。Future 的结果是 Permit 或 None。"""

    call: ToolCallContext
    future: asyncio.Future[ToolPermit | None]


class ToolPermit:
    """一次准入许可；释放操作幂等。

    Permit 代表一个已经计入全局和单运行计数的执行名额。执行器拿到它后，
    无论工具成功、失败还是被取消，都必须调用 ``release`` 归还名额。
    """

    def __init__(self, admission: ToolAdmission, run_id: str) -> None:
        self._admission = admission
        self.run_id = run_id
        self._released = False

    def release(self) -> None:
        # finally 中可能重复触发清理，因此这里不能让同一 Permit 重复扣减计数。
        if self._released:
            return
        self._released = True
        self._admission._release(self)


class ToolAdmission:
    """单事件循环内的全局/单运行共享准入器。

    它只管理“能否进入执行阶段”，不执行工具，也不处理工具结果。这样容量、
    排队和业务执行可以分别测试和维护。
    """

    def __init__(
        self,
        *,
        global_limit: int = 3,
        per_run_limit: int = 3,
        max_pending: int = 30,
        max_pending_per_run: int = 6,
        admission_timeout: float = 30.0,
    ) -> None:
        for name, value in (
            ("global_limit", global_limit),
            ("per_run_limit", per_run_limit),
            ("max_pending", max_pending),
            ("max_pending_per_run", max_pending_per_run),
        ):
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if max_pending_per_run > max_pending:
            raise ValueError("max_pending_per_run 不能大于 max_pending")
        if not isinstance(admission_timeout, (int, float)) or admission_timeout <= 0:
            raise ValueError("admission_timeout 必须是正数")

        self.global_limit = global_limit
        self.per_run_limit = per_run_limit
        self.max_pending = max_pending
        self.max_pending_per_run = max_pending_per_run
        self.admission_timeout = float(admission_timeout)
        # active 是所有运行合计的在途数量；active_by_run 用来限制单个运行。
        self._active = 0
        self._active_by_run: dict[str, int] = {}
        # pending 是等待 Permit 的总数；每运行计数用于防止一个运行占满队列。
        self._pending = 0
        self._pending_by_run: dict[str, int] = {}
        # 每个运行一条 FIFO 队列；run_order 保存当前有等待者的运行顺序。
        self._queues: dict[str, deque[_Waiter]] = {}
        self._run_order: deque[str] = deque()
        self._queued_runs: set[str] = set()
        # 记录上一次放行的运行，轮转时从下一个运行开始，避免单运行连续霸占队头。
        self._last_granted_run: str | None = None
        self._closed = False

    @property
    def active(self) -> int:
        return self._active

    @property
    def pending(self) -> int:
        return self._pending

    async def acquire(self, call: ToolCallContext) -> ToolPermit | None:
        """等待并取得许可；队列满或准入等待耗尽返回 ``None``。

        等待期间同时监听调用取消、运行停止、绝对 deadline 和准入等待上限。
        类型化控制信号会抛出异常；单纯容量耗尽返回 ``None``，由上层转换为
        ``CAPACITY_EXCEEDED``，表示调用尚未开始。
        """

        # 先检查调用是否已经被取消、超时以及停止运行，避免无谓排队。
        self._check_abort(call)

        # 先检查准入是否已经关闭，避免无谓排队。
        if self._closed:
            raise ToolRunStoppedError(
                "工具准入已关闭",
                run_id=call.run_id,
                operation_id=call.operation_id,
            )

        # 先拒绝超出队列容量的调用，避免无界增长；此时尚未创建等待者。
        if self._pending >= self.max_pending or (self._pending_by_run.get(call.run_id, 0) >= self.max_pending_per_run):
            return None

        # 创建等待者并入队。
        loop = asyncio.get_running_loop()
        waiter = _Waiter(call=call, future=loop.create_future())
        queue = self._queues.setdefault(call.run_id, deque())
        queue.append(waiter)

        # 计入全局和单运行等待计数，并将该运行加入轮转队列。
        self._pending += 1
        self._pending_by_run[call.run_id] = self._pending_by_run.get(call.run_id, 0) + 1
        self._enqueue_run(call.run_id)

        # 新请求入队后立即尝试放行；如果有空闲名额，通常无需真正等待。
        self._pump()

        # _pump() 在上面已经尝试过一次即时放行：如果当时存在空闲名额，
        # 它会同步给 Future 写入 ToolPermit（准入器关闭时也可能写入 None）。
        # Future.done() 只表示结果已经准备好，不会再次等待；此时直接取结果，
        # 避免为已经获得结果的调用额外创建取消、deadline 和准入超时监听任务。
        if waiter.future.done():
            return await waiter.future

        # 只有当 waiter.future 已返回 Permit 时，调用方才拥有该 Permit。
        # 这个变量用于处理“取消与放行同时发生”的竞态。
        owned_permit: ToolPermit | None = None

        # 监听调用取消、运行停止、绝对 deadline 和准入等待上限，谁先发生就先处理谁。
        control_tasks: set[asyncio.Task[object]] = set()
        watched = (*call.cancel_events, call.run_stop)
        for event in watched:
            control_tasks.add(asyncio.create_task(event.wait()))
        deadline_task = asyncio.create_task(self._sleep_until(call.deadline))
        timeout_task = asyncio.create_task(asyncio.sleep(self.admission_timeout))
        control_tasks.update((deadline_task, timeout_task))
        try:
            # Future 与所有终止条件竞争，谁先完成就先处理谁。
            done, _ = await asyncio.wait(
                {waiter.future, *control_tasks},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if waiter.future in done:
                owned_permit = await waiter.future
                return owned_permit
            if any(task in done for task in control_tasks):
                self._check_abort(call)
                # 仅准入预算耗尽：该 attempt 没有被调度，不进入工具重试。
                return None
            return None
        finally:
            await self._finalize_waiter(waiter, control_tasks, owned_permit)

    async def _finalize_waiter(
        self,
        waiter: _Waiter,
        control_tasks: set[asyncio.Task[object]],
        owned_permit: ToolPermit | None,
    ) -> None:
        """收尾一次排队等待，并处理取消与放行同时发生的竞态。

        ``asyncio.wait`` 不会自动清理未完成的监听任务，因此先取消并等待它们；
        随后移除等待者。若 Future 已经产生 Permit、但调用方尚未取得它，说明
        放行与取消发生在同一时刻，由这里代为释放，避免共享容量永久被占用。
        ``owned_permit`` 非空时，Permit 所有权已经转移给调用方，不在此重复释放。
        """
        # asyncio.wait 不会自动清理其他任务，必须逐个取消并等待，避免后台任务泄漏。
        for task in control_tasks:
            if not task.done():
                task.cancel()
        if control_tasks:
            await asyncio.gather(*control_tasks, return_exceptions=True)
        self._remove_waiter(waiter)

        # 取消/准入超时与放行同刻发生时，调用方不会拿到 Permit；这里代为回收。
        if owned_permit is None and waiter.future.done() and not waiter.future.cancelled():
            permit = waiter.future.result()
            if permit is not None and not permit._released:
                permit.release()

    async def close_run(self, run_id: str) -> None:
        """撤回某运行尚未取得的排队请求。

        只撤回等待者，不触碰已经取得 Permit 的在途调用；后者仍由执行器负责释放。
        """
        queue = self._queues.pop(run_id, None)
        if queue is None:
            return
        while queue:
            waiter = queue.popleft()
            self._forget_pending(waiter.call.run_id)
            if not waiter.future.done():
                waiter.future.set_result(None)
        self._queued_runs.discard(run_id)
        self._run_order = deque(item for item in self._run_order if item != run_id)
        self._pump()

    async def close(self) -> None:
        """停止新准入并撤回所有排队请求；已取得许可由持有者释放。"""
        self._closed = True
        for run_id in tuple(self._queues):
            await self.close_run(run_id)

    def _pump(self) -> None:
        """从运行队列中放行等待者，直到容量耗尽或没有可运行的队列。

        同一运行内部保持 FIFO；运行之间按轮转调度。单运行达到在途上限时，
        暂时跳过该运行，让其他运行继续使用全局剩余容量。
        """
        # 只要全局容量未满且还有排队运行，就尝试放行等待者。
        while self._active < self.global_limit and self._run_order:
            # 从上一次获准运行的下一个队列开始，避免同一运行持续占据轮转头部。
            if self._last_granted_run in self._queued_runs and len(self._run_order) > 1:
                while self._run_order[0] == self._last_granted_run:
                    self._run_order.rotate(-1)

            made_progress = False
            for _ in range(len(self._run_order)):
                # 取出队列头部的运行 ID，并从“已经排入调度顺序”的标记集合中移除它。
                run_id = self._run_order.popleft()
                self._queued_runs.discard(run_id)

                # 取出该运行的等待队列；如果队列为空，直接丢弃它，继续轮转。
                queue = self._queues.get(run_id)
                if not queue:
                    self._queues.pop(run_id, None)
                    continue

                # 如果该运行已经达到在途上限，暂时跳过它并将其重新加入调度队列，让其他运行继续使用全局剩余容量。
                if self._active_by_run.get(run_id, 0) >= self.per_run_limit:
                    self._enqueue_run(run_id)
                    continue

                waiter = queue.popleft()
                self._forget_pending(run_id)
                # 只有在队列仍有等待者时，才将该运行重新加入调度队列。
                if queue:
                    self._enqueue_run(run_id)
                else:
                    self._queues.pop(run_id, None)

                # Future 已经有终态（即当前等待者的准入结果已经确定）时不能再发放 Permit；
                # 丢弃该等待者并继续寻找可放行者。
                if waiter.future.done():
                    made_progress = True
                    continue

                # 先增加计数再设置 Future，避免 Future 回调立即触发时看到旧计数。
                self._active += 1
                self._active_by_run[run_id] = self._active_by_run.get(run_id, 0) + 1
                self._last_granted_run = run_id
                waiter.future.set_result(ToolPermit(self, run_id))

                # 只放行一个等待者后就退出轮转，避免同一轮次连续放行多个等待者，导致单运行霸占容量。
                made_progress = True
                break

            if not made_progress:
                break

    def _release(self, permit: ToolPermit) -> None:
        """归还一个 Permit，并立即尝试放行下一位等待者。"""
        self._active = max(0, self._active - 1)
        active = self._active_by_run.get(permit.run_id, 0) - 1
        if active > 0:
            self._active_by_run[permit.run_id] = active
        else:
            self._active_by_run.pop(permit.run_id, None)
        self._pump()

    def _enqueue_run(self, run_id: str) -> None:
        if run_id not in self._queued_runs:
            self._queued_runs.add(run_id)
            self._run_order.append(run_id)

    def _remove_waiter(self, waiter: _Waiter) -> None:
        """从等待队列移除已取消或已超时的请求，并恢复调度。"""
        queue = self._queues.get(waiter.call.run_id)
        if queue is None:
            return
        try:
            queue.remove(waiter)
        except ValueError:
            return
        self._forget_pending(waiter.call.run_id)
        if not queue:
            self._queues.pop(waiter.call.run_id, None)
            self._queued_runs.discard(waiter.call.run_id)
            self._run_order = deque(item for item in self._run_order if item != waiter.call.run_id)
        self._pump()

    def _forget_pending(self, run_id: str) -> None:
        """同步减少全局和单运行等待计数。"""
        self._pending = max(0, self._pending - 1)
        pending = self._pending_by_run.get(run_id, 0) - 1
        if pending > 0:
            self._pending_by_run[run_id] = pending
        else:
            self._pending_by_run.pop(run_id, None)

    @staticmethod
    async def _sleep_until(deadline: float | None) -> None:
        """把绝对 monotonic deadline 转换成可竞争的异步等待。"""
        if deadline is None:
            await asyncio.Future()
        else:
            delay = deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

    @staticmethod
    def _check_abort(call: ToolCallContext) -> None:
        """执行准入前后的快速检查，统一生成领域可识别的控制异常。"""
        if any(event.is_set() for event in call.cancel_events):
            raise ToolCancelledError(
                "工具调用已取消",
                run_id=call.run_id,
                operation_id=call.operation_id,
            )
        if call.deadline is not None and time.monotonic() >= call.deadline:
            raise ToolDeadlineExceededError(
                "工具调用期限已到",
                run_id=call.run_id,
                operation_id=call.operation_id,
            )
        if call.run_stop.is_set():
            raise ToolRunStoppedError(
                "所属运行已停止新的工具调用",
                run_id=call.run_id,
                operation_id=call.operation_id,
            )
