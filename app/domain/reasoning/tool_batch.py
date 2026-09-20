"""领域工具批次的事实收集与并行结果接管。"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, TypeVar

from app.domain.ports.tool_execution import (
    ToolCallContext,
    ToolCleanupState,
    ToolEffectState,
    ToolExecutionState,
    ToolFact,
)
from app.shared.exceptions import ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError

_Result = TypeVar("_Result")
_CONTROL_ERRORS = (ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError)
# 兄弟报出控制异常后，留给其他在途兄弟收尾的上界（实际取其与 cleanup_deadline 的较小值）。
# 默认值与配置规格的 tool_batch_cleanup_grace_seconds 一致；生产值由 Container 注入到
# ExecutionLimits 后经 `run` 传入，本常量只在直接调用方未提供时兜底。
DEFAULT_BATCH_CLEANUP_GRACE = 1.0


class ToolBatchCollector:
    """按当前消费调用及规范 attempt/revision 保存批次事实快照。"""

    def __init__(self) -> None:
        self._facts: dict[tuple[str, str, str, str | None], ToolFact] = {}
        self._closed = False

    def record(self, fact: ToolFact) -> bool:
        """幂等接管较新的事实；关闭后拒绝继续持有 Integration 更新。"""
        if self._closed:
            return False
        key = (
            fact.batch_id,
            fact.tool_call_id,
            fact.operation_id,
            fact.attempt_id,
        )
        current = self._facts.get(key)
        if current is None or fact.revision > current.revision:
            self._facts[key] = copy.deepcopy(fact)
        return True

    def snapshot(self) -> tuple[ToolFact, ...]:
        """返回调用方专有副本，避免其修改污染收集器中的事实。"""
        return tuple(copy.deepcopy(fact) for fact in self._facts.values())

    def close(self) -> None:
        """停止接收晚到更新；已接管快照仍可读取。"""
        self._closed = True


class ToolBatchRunner:
    """只协调本批次的并行任务；事实归 Collector，协议提交归策略。"""

    def __init__(self, collector: ToolBatchCollector) -> None:
        self._collector = collector
        self.outcomes: Sequence[object] = []

    async def run(
        self,
        calls: Sequence[ToolCallContext],
        execute: Callable[[int], Coroutine[Any, Any, _Result]],
        *,
        cleanup_deadline: float | None = None,
        batch_cleanup_grace: float = DEFAULT_BATCH_CLEANUP_GRACE,
    ) -> list[_Result | BaseException | None]:
        """先预登记全部调用，再逐个接管完成值；控制异常留给调用方选择终态。

        `batch_cleanup_grace` 是首个控制异常后给在途兄弟的收尾上界，实际取它与
        `cleanup_deadline` 的较小值；由调用方按运行配置传入，本组件不读配置。
        """
        for call in calls:
            # 预登记：为每个 call 写入 revision=0 的 NOT_STARTED 事实。目的是让"从未启动"也有事实可查，
            # revision=0 保证任何真实事实（revision>=1）都能覆盖它。
            self._collector.record(
                ToolFact(
                    operation_id=call.operation_id,
                    run_id=call.run_id,
                    batch_id=call.batch_id,
                    tool_call_id=call.tool_call_id,
                    revision=0,
                    execution_state=ToolExecutionState.NOT_STARTED,
                    effect_state=ToolEffectState.NONE,
                    cleanup_state=ToolCleanupState.NOT_NEEDED,
                )
            )

        outcomes: list[_Result | BaseException | None] = [None] * len(calls)
        self.outcomes = outcomes
        tasks = {asyncio.create_task(execute(index)): index for index in range(len(calls))}
        pending = set(tasks)
        stop_at: float | None = None

        def absorb(done: set[asyncio.Task[_Result]]) -> None:
            """在每个任务完成时立刻把返回值或异常对象写进 outcomes[index]，不等整批成功"""
            nonlocal stop_at
            for task in done:
                index = tasks[task]
                try:
                    outcomes[index] = task.result()
                except BaseException as error:  # noqa: BLE001 -- 控制与硬取消都留给批次出口裁决
                    outcomes[index] = error  # 原样保存异常，终态由调用方裁决
                    # 首个控制异常启动宽限；stop_at is None 守卫使多个兄弟同时报异常时不叠加
                    if isinstance(error, _CONTROL_ERRORS) and stop_at is None:
                        stop_at = min(
                            time.monotonic() + batch_cleanup_grace,
                            cleanup_deadline if cleanup_deadline is not None else float("inf"),
                        )

        try:
            while pending:
                # remaining 的三种取值：None=无限等待（尚无控制异常）；0=即时轮询（宽限已用尽）；
                # 0<remaining<=宽限=挂起协程等待剩余宽限（已有控制异常且未到期）。
                remaining = None if stop_at is None else max(0.0, stop_at - time.monotonic())

                # 整个批次并发真正发生的地方：asyncio.wait 给每个 pending 任务挂完成回调、按需挂定时器，
                # 并挂起当前协程把控制权交还事件循环。
                # 三种唤醒：任一任务完成→done 非空；定时器到期→done 空；外层取消→抛 CancelledError。
                done, pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)

                absorb(done)

                # 空 done 只可能来自定时器到期，是宽限耗尽的唯一出口。
                # 注意 wait 超时不抛异常、不取消任务；wait_for 才会抛 TimeoutError 并取消目标任务。
                if not done:
                    break
        except asyncio.CancelledError:
            # 外层 task.cancel() 不是批次结果；先给已有真实执行一个有界清理机会，再原样传播。
            # 已有控制异常时沿用其剩余宽限：重设会让硬取消再整取一份宽限，收尾窗口翻倍。
            if stop_at is None:
                stop_at = min(
                    time.monotonic() + batch_cleanup_grace,
                    cleanup_deadline if cleanup_deadline is not None else float("inf"),
                )
            raise
        finally:
            # 只有"宽限耗尽"和"外层硬取消"会带着未退出的任务走到这里；正常跑完时 pending 为空。
            if pending:
                for task in pending:
                    task.cancel()
                # 宽限耗尽时 stop_at 已过期，remaining == 0（取消后不再等）；硬取消时是剩余宽限。
                remaining = max(0.0, (stop_at or time.monotonic()) - time.monotonic())

                # 取消后的结局分两类：合作取消的任务被这次等待捕获（异常已写入 outcomes）；
                # 吞掉取消的任务留在 pending（真实线程未停），outcomes 保持 None。
                done, pending = await asyncio.wait(pending, timeout=remaining)
                absorb(done)

                # 仍留在 pending 的任务不在本批次等待范围内，其真实执行归 Integration。
                # 注册回调只为将来取走异常，避免 asyncio 报 "exception was never retrieved"；
                # 回调不引用 Agent 或 collector，不延长其寿命。
                for task in pending:
                    task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)

        return outcomes
