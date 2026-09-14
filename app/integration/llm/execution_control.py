"""
LLM 层内部执行控制等待原语（LLM-044：retry / 整流 / request_execution 共用）。

执行终止信号（`_StreamCancel` / `_DeadlineExceeded`）驱动的等待原语，供：
    - retry 退避等待（RetryHandler.execute）
    - 整流 / 续接退避（StreamingRectifier._backoff_sleep）
    - reserve 排队 + 每次真实 create 的受控 await（request_execution._budget_guarded_call）
共用。定义于本模块（而非 errors.py / retry.py）避免组件间反向依赖：各 llm 内部
组件依赖本模块，本模块只依赖 `.errors` 的私有信号类型。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from .errors import _DeadlineExceeded, _StreamCancel

_AbortReason = Literal["cancel", "deadline"]


# =====================================================================
# 执行控制等待辅助（LLM-044：retry / 整流 / request_execution 共用）
# =====================================================================


def _raise_if_aborted(
    cancel_event: asyncio.Event | None, deadline: float | None
) -> None:
    """真实请求前快检：已取消 / 已到期 → 抛类型化终止信号（不发起后续请求）。"""
    if cancel_event is not None and cancel_event.is_set():
        raise _StreamCancel()
    if deadline is not None and time.monotonic() >= deadline:
        raise _DeadlineExceeded()


async def _abort_trigger(
    cancel_event: asyncio.Event | None, deadline: float | None
) -> _AbortReason:
    """等待 cancel_event 置位或 deadline（monotonic 绝对）到期，返回 'cancel'/'deadline'。

    cancel 优先于 deadline（二者同刻以取消为准）；两者皆无信号则永不返回（挂起至被取消）。
    内部 waiter task 在 finally 取消并回收（防后台泄漏）。
    """
    tasks: dict[_AbortReason, asyncio.Task] = {}
    if cancel_event is not None:
        tasks["cancel"] = asyncio.ensure_future(cancel_event.wait())
    if deadline is not None:
        tasks["deadline"] = asyncio.ensure_future(
            asyncio.sleep(max(0.0, deadline - time.monotonic()))
        )
    if not tasks:
        await asyncio.Event().wait()  # pragma: no cover —— 无信号调用方不应走此分支
    try:
        done, _ = await asyncio.wait(
            tasks.values(), return_when=asyncio.FIRST_COMPLETED
        )
        if "cancel" in tasks and tasks["cancel"] in done:
            return "cancel"
        return "deadline"
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def wait_with_execution_control(
    delay: float,
    *,
    cancel_event: asyncio.Event | None = None,
    deadline: float | None = None,
) -> None:
    """退避/整流等待：正常睡满 delay 后返回；期间 cancel/deadline 先到 → 抛类型化终止信号。

    - deadline 是调用方传入的 monotonic 绝对时刻，不重算时长；
    - deadline 到期不以内置 TimeoutError 表现（避免被 classify_error 当网络超时 RETRYABLE）；
    - 中断胜出后 cancel + await 内部等待 task（防泄漏）。
    """
    if cancel_event is None and deadline is None:
        if delay > 0:
            await asyncio.sleep(delay)
        return
    sleep_task = asyncio.ensure_future(asyncio.sleep(delay))
    abort_task = asyncio.ensure_future(_abort_trigger(cancel_event, deadline))
    try:
        done, _ = await asyncio.wait(
            {sleep_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if abort_task in done:
            reason = abort_task.result()
            if reason == "cancel":
                raise _StreamCancel()
            raise _DeadlineExceeded()
        # sleep 正常完成
    finally:
        for task in (sleep_task, abort_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(sleep_task, abort_task, return_exceptions=True)


async def await_with_execution_control[T](
    factory: Callable[[], Awaitable[T]],
    *,
    cancel_event: asyncio.Event | None = None,
    deadline: float | None = None,
) -> T:
    """受控 await 一次真实请求前/中的协程**工厂**（reserve / create 共用，LLM-044）。

    收工厂而非已建 coroutine：入口快检命中直接抛时，工厂不会被调用、未 await 的
    coroutine 不存在（杜绝 "was never awaited"）。

    返回与终止的约定（LLM-045）：**abort 决定业务终态、迟回值决定资源所有权，两者
    不互斥**——调用方须先接管返回值（settle/cancel/关流），再完成 abort 收尾；helper
    返回任何值都**不代表业务未终止**（迟回值不是业务成功）。规则：
    - 快检已终止 → 抛类型化信号，工厂不执行（请求未发）；
    - task 与终止**同时完成** → 优先取回 task 结果返回（调用方复查决定取消/结算，不丢返回值）；
    - 终止先到 → cancel task 并 await 收尾，按 task 结局分派：
        - task 重抛 CancelledError（配合取消、内部已清理），或清理期抛其他 Exception
          → 抛类型化信号（cancel 命中 _StreamCancel / deadline 命中 _DeadlineExceeded；
          清理期普通异常被吞、不覆盖 abort）；
        - task **吞取消以值收尾**（或 cancel 因 task 已收尾而 no-op、await 取得迟回值）
          → **返回该值**：资源所有权移交调用方，由调用方 abort 复查接管并完成业务收尾
          （reserve→cancel 退款 / create→关流或结算），与「同时完成」同一所有权路径，
          不丢返回值；
    - 其余 BaseException（SystemExit 等）不吞、原样上抛；全部 waiter task 在 finally 取消回收。

    调用方前提（helper 不独立保证「返回即未终止」）：`cancel_event` 单次调用内须为
    单向置位信号（命中后不得 clear 复用）；任何返回值到达即代表所有权已移交调用方，
    须在产生下一项外部副作用前复查 cancel/deadline；迟回值不得直接当作业务成功。
    """
    # 入口快检，如果已经取消 / 超时，直接抛异常，**`factory()` 根本不执行**，不会创建业务协程。
    _raise_if_aborted(cancel_event, deadline)
    task = asyncio.ensure_future(factory())
    abort_task = asyncio.ensure_future(_abort_trigger(cancel_event, deadline))
    try:
        done, _ = await asyncio.wait(
            {task, abort_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:  # 业务任务和终止信号同时完成，优先返回业务结果！
            return task.result()
        reason = abort_task.result()
        task.cancel()
        try:
            result = await task
        except asyncio.CancelledError:
            # 工厂配合取消、内部清理后重抛 → abort 收尾（现状保持）
            pass
        except Exception:  # noqa: BLE001, S110
            # 取消后清理期真实异常 → abort 优先：吞掉、抛类型化信号（现状保持）
            pass
        else:
            # 吞取消以值收尾 / cancel 因 task 已收尾而 no-op、await 取得迟回值 →
            # 请求实际已发生 / 资源已取得：返回该值，交调用方 abort 复查接管资源并
            # 完成业务收尾（先接管、再收尾，LLM-045）。迟回值不是业务成功。
            return result
        if reason == "cancel":
            raise _StreamCancel()
        raise _DeadlineExceeded()
    finally:
        for t in (task, abort_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(task, abort_task, return_exceptions=True)
