"""
任务调度服务

职责：
- 限制并发 Agent 任务数（asyncio.Semaphore，对应 GPU/服务器资源）
- 提供 run_agent() 包装：在信号量保护下运行 Agent，流式产出 SSE 事件
- 按 run 保存独立取消 Event，并维护 session 到活动 run 的索引

并发信号量是 Agent 维度（限制同时运行的 Agent 任务），而非 LLM API 维度
（RPM/TPM 由 reservation_limiter 覆盖）。对应配置：agent_max_concurrent_tasks。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.agent.base import AgentContext
    from app.domain.agent.executor import ReActAgent


class TaskService:
    """
    任务调度服务：限制并发 Agent 任务数。

    信号量在 run_agent 的 generator 外 acquire/release——yield 会挂起
    generator frame，若 acquire 放 generator 内，其他任务会在首个 yield
    前交错进入，信号量失去约束。
    """

    def __init__(self, max_concurrent: int = 10) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # 会话级取消事件注册表（/chat/stop 置位 → 运行中的 Agent 感知取消）
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._session_runs: dict[str, set[str]] = {}
        self._run_sessions: dict[str, str] = {}

    @property
    def max_concurrent(self) -> int:
        """最大并发任务数。"""
        return self._semaphore._value

    # ===== 活动运行取消索引（run 隔离、session 批量取消） =====

    def create_cancel_event(self, session_id: str, run_id: str) -> asyncio.Event:
        """为 Application 已创建的 run 登记独立取消事件。"""
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串")
        if run_id in self._cancel_events:
            raise ValueError(f"run_id 已登记: {run_id}")
        ev = asyncio.Event()
        self._cancel_events[run_id] = ev
        self._run_sessions[run_id] = session_id
        self._session_runs.setdefault(session_id, set()).add(run_id)
        return ev

    def get_cancel_event(self, run_id: str) -> asyncio.Event | None:
        """按运行身份获取取消事件。"""
        return self._cancel_events.get(run_id)

    def clear_cancel_event(self, run_id: str) -> None:
        """仅释放指定运行，不能误删同会话兄弟运行。"""
        self._cancel_events.pop(run_id, None)
        session_id = self._run_sessions.pop(run_id, None)
        if session_id is None:
            return
        runs = self._session_runs.get(session_id)
        if runs is None:
            return
        runs.discard(run_id)
        if not runs:
            self._session_runs.pop(session_id, None)

    def cancel_session(self, session_id: str) -> bool:
        """置位会话取消事件（/chat/stop）；无运行任务返回 False。"""
        run_ids = tuple(self._session_runs.get(session_id, ()))
        if not run_ids:
            return False
        for run_id in run_ids:
            event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()
        return True

    async def run_agent(
        self,
        user_input: str,
        messages: list[dict],
        context: AgentContext,
        agent: ReActAgent,
    ) -> AsyncGenerator[str]:
        """
        在信号量保护下运行 Agent，流式产出 SSE 事件。

        Args:
            user_input: 用户输入
            messages: 组装好的消息列表
            context: Agent 上下文
            agent: ReActAgent 实例（持有运行期结果，每次调用独立创建）

        Yields:
            SSE 事件字符串

        并发超限时在此等待。退出时先显式关闭 Agent 子生成器，再由信号量
        上下文释放并发许可；异常、取消和消费者提前关闭走同一收尾顺序。
        """
        async with (
            self._semaphore,
            aclosing(agent.run(user_input, messages, context)) as stream,
        ):
            async for event in stream:
                yield event
