"""
任务调度服务

职责：
- 限制并发 Agent 任务数（asyncio.Semaphore，对应 GPU/服务器资源）
- 提供 run_agent() 包装：在信号量保护下运行 Agent，流式产出 SSE 事件

并发信号量是 Agent 维度（限制同时运行的 Agent 任务），而非 LLM API 维度
（RPM/TPM 由 reservation_limiter 覆盖）。对应配置：agent_max_concurrent_tasks。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.core.agent.base import AgentContext
    from app.core.agent.executor import ReActAgent


class TaskService:
    """
    任务调度服务：限制并发 Agent 任务数。

    信号量在 run_agent 的 generator 外 acquire/release——yield 会挂起
    generator frame，若 acquire 放 generator 内，其他任务会在首个 yield
    前交错进入，信号量失去约束。
    """

    def __init__(self, max_concurrent: int | None = None) -> None:
        self._semaphore = asyncio.Semaphore(
            max_concurrent or settings.agent_max_concurrent_tasks
        )

    @property
    def max_concurrent(self) -> int:
        """最大并发任务数。"""
        return self._semaphore._value

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
            agent: ReActAgent 实例（每次调用新建，无状态）

        Yields:
            SSE 事件字符串

        并发超限时在此等待（async with 天然保证异常/取消时释放信号量，
        不会挂死占坑）。
        """
        async with self._semaphore:
            async for event in agent.run(user_input, messages, context):
                yield event
