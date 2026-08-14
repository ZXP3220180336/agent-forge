"""
TaskService 单元测试

覆盖：
    任务级并发信号量：同时运行 Agent 任务数不超过 agent_max_concurrent_tasks
    run_agent：流式转发 + 信号量保护（async with 异常/取消释放）
"""

import asyncio

import pytest

from app.config import settings
from app.domain.agent.base import AgentContext
from app.application.task.task_service import TaskService


class _FakeAgent:
    """最小 Agent 替身：run() 是 async generator，可配置延迟与事件数。"""

    def __init__(self, delay: float = 0.05, events: int = 1) -> None:
        self.delay = delay
        self.events = events
        self.runs = 0

    async def run(self, user_input, messages, context):
        self.runs += 1
        for i in range(self.events):
            await asyncio.sleep(self.delay)
            yield f"data: event{i}\n\n"


@pytest.mark.asyncio
async def test_task_service_limits_concurrency(monkeypatch):
    """并发 Agent 任务数不超过信号量上限。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tasks", 2)
    ts = TaskService(max_concurrent=2)

    # 用共享计数：只有信号量放行后（run() 内部）才递增，观测真正并发
    state = {"active": 0, "max_active": 0}

    class _TrackingAgent(_FakeAgent):
        async def run(self, user_input, messages, context):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            try:
                async for ev in super().run(user_input, messages, context):
                    yield ev
            finally:
                state["active"] -= 1

    async def run_one(i):
        fake = _TrackingAgent(delay=0.02)
        async for _ in ts.run_agent(
            user_input=str(i),
            messages=[],
            context=AgentContext(session_id="s", user_id="u"),
            agent=fake,
        ):
            pass

    await asyncio.gather(*[run_one(i) for i in range(5)])

    assert state["max_active"] <= 2, f"并发数应受信号量限制（2），实际 {state['max_active']}"


@pytest.mark.asyncio
async def test_run_agent_forwards_events():
    """run_agent 流式转发 Agent 事件。"""
    ts = TaskService(max_concurrent=2)
    fake = _FakeAgent(delay=0, events=3)
    events = []
    async for ev in ts.run_agent(
        user_input="hi",
        messages=[],
        context=AgentContext(session_id="s", user_id="u"),
        agent=fake,
    ):
        events.append(ev)
    assert len(events) == 3, f"应转发 3 个事件，实际 {len(events)}"
    assert fake.runs == 1


@pytest.mark.asyncio
async def test_run_agent_releases_semaphore_on_error():
    """异常时信号量仍释放（async with 保证）：异常后能再进入。"""
    ts = TaskService(max_concurrent=2)

    class _FailingAgent:
        async def run(self, user_input, messages, context):
            yield "data: start\n\n"
            raise RuntimeError("boom")

    async def consume():
        events = []
        try:
            async for ev in ts.run_agent(
                user_input="x",
                messages=[],
                context=AgentContext(session_id="s", user_id="u"),
                agent=_FailingAgent(),
            ):
                events.append(ev)
        except RuntimeError:
            pass
        return events

    # 第一次运行异常，信号量应已释放；第二次可正常进入
    events1 = await consume()
    assert events1 == ["data: start\n\n"]
    events2 = await consume()
    assert events2 == ["data: start\n\n"], "信号量应在异常后释放"


@pytest.mark.asyncio
async def test_max_concurrent_property():
    """max_concurrent 属性反映配置值。"""
    ts = TaskService(max_concurrent=4)
    assert ts.max_concurrent == 4
