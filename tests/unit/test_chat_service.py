"""ChatService 应用边界与运行所有权测试。"""

import pytest

from app.application.chat import ChatService
from app.application.task.task_service import TaskService
from app.domain.agent import AgentResult
from app.shared.exceptions import ForbiddenError, NotFoundError


class _SessionManager:
    def __init__(self, session: dict | None) -> None:
        self.session = session
        self.saved: list[dict] = []

    async def get_session(self, session_id):
        return self.session

    async def add_message(
        self,
        session_id,
        role,
        content,
        reasoning_content=None,
        token_count=0,
    ) -> int:
        self.saved.append(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "reasoning_content": reasoning_content,
                "token_count": token_count,
            }
        )
        return len(self.saved)


class _ContextManager:
    def __init__(self) -> None:
        self.build_calls: list[dict] = []

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def build_messages(self, **kwargs):
        self.build_calls.append(kwargs)
        return ([{"role": "user", "content": kwargs["user_message"]}], 1, 0)

    def trim_messages(self, messages, max_rounds=None, max_tokens=None):
        return None


def _service(session: dict | None, agent_params: dict):
    sessions = _SessionManager(session)
    context = _ContextManager()
    tasks = TaskService()
    service = ChatService(
        session_manager=sessions,
        context_manager=context,
        task_service=tasks,
        llm=object(),
        tools=object(),
        agent_params=agent_params,
    )
    return service, sessions, context, tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "error"),
    [
        (None, NotFoundError),
        ({"id": "s1", "user_id": "other"}, ForbiddenError),
    ],
)
async def test_prepare_rejects_before_message_or_run_side_effects(
    session,
    error,
    agent_params,
):
    """不存在和越权均在用户消息、上下文及运行登记前拒绝。"""
    service, sessions, context, tasks = _service(session, agent_params)

    with pytest.raises(error):
        await service.prepare_message(
            session_id="s1",
            user_id="user_x",
            message="hello",
            max_iterations=None,
        )

    assert sessions.saved == []
    assert context.build_calls == []
    assert tasks.cancel_session("s1") is False


@pytest.mark.asyncio
async def test_prepare_uses_message_id_and_isolates_same_session_runs(agent_params):
    """当前消息 ID 形成历史快照上界；同会话运行独立登记和清理。"""
    service, sessions, context, tasks = _service(
        {"id": "s1", "user_id": "user_x", "system_prompt": "sys"},
        agent_params,
    )

    first = await service.prepare_message(
        session_id="s1",
        user_id="user_x",
        message="first",
        max_iterations=3,
    )
    second = await service.prepare_message(
        session_id="s1",
        user_id="user_x",
        message="second",
        max_iterations=None,
    )

    assert first.run_id != second.run_id
    assert first.context.run_id == first.run_id
    assert first.context.max_iterations == 3
    assert second.context.max_iterations == agent_params["max_iterations"]
    assert context.build_calls[0]["current_message_id"] == 1
    assert context.build_calls[1]["current_message_id"] == 2

    await first.aclose()
    assert tasks.get_cancel_event(first.run_id) is None
    assert tasks.get_cancel_event(second.run_id) is second.cancel_event
    assert service.cancel_session("s1") is True
    assert second.cancel_event.is_set()
    await second.aclose()
    assert tasks.cancel_session("s1") is False


@pytest.mark.asyncio
async def test_close_persists_non_empty_result_once_and_clears_before_failure(
    agent_params,
):
    """收尾至多保存一次完整答复，并先释放取消登记。"""
    service, sessions, _context, tasks = _service(
        {"id": "s1", "user_id": "user_x", "system_prompt": "sys"},
        agent_params,
    )
    run = await service.prepare_message(
        session_id="s1",
        user_id="user_x",
        message="question",
        max_iterations=None,
    )
    run.agent._result = AgentResult(
        success=True,
        content="  answer  ",
        reasoning="reason",
    )

    await run.aclose()
    await run.aclose()

    assert tasks.get_cancel_event(run.run_id) is None
    assert [item["role"] for item in sessions.saved] == ["user", "assistant"]
    assert sessions.saved[-1]["content"] == "answer"
    assert sessions.saved[-1]["reasoning_content"] == "reason"


@pytest.mark.asyncio
async def test_agent_construction_failure_clears_registered_run(
    monkeypatch,
    agent_params,
):
    """运行登记后的 Agent 构造失败不能遗留活动运行。"""
    service, _sessions, _context, tasks = _service(
        {"id": "s1", "user_id": "user_x", "system_prompt": "sys"},
        agent_params,
    )

    def fail_agent(**kwargs):
        raise RuntimeError("agent init failed")

    monkeypatch.setattr(
        "app.application.chat.chat_service.ReActAgent",
        fail_agent,
    )

    with pytest.raises(RuntimeError, match="agent init failed"):
        await service.prepare_message(
            session_id="s1",
            user_id="user_x",
            message="question",
            max_iterations=None,
        )

    assert tasks.cancel_session("s1") is False


@pytest.mark.asyncio
async def test_prepare_rejects_non_positive_message_id(agent_params):
    """消息 ID 缺失时不静默关闭历史快照边界。"""

    class _ZeroIdSessionManager(_SessionManager):
        async def add_message(self, *args, **kwargs) -> int:
            await super().add_message(*args, **kwargs)
            return 0

    sessions = _ZeroIdSessionManager({"id": "s1", "user_id": "user_x", "system_prompt": "sys"})
    context = _ContextManager()
    tasks = TaskService()
    service = ChatService(
        session_manager=sessions,
        context_manager=context,
        task_service=tasks,
        llm=object(),
        tools=object(),
        agent_params=agent_params,
    )

    with pytest.raises(RuntimeError, match="有效消息 ID"):
        await service.prepare_message(
            session_id="s1",
            user_id="user_x",
            message="question",
            max_iterations=None,
        )

    assert context.build_calls == []
    assert tasks.cancel_session("s1") is False


@pytest.mark.asyncio
async def test_assistant_persistence_failure_still_clears_run(agent_params):
    """assistant 关键提交失败会向上抛出，但运行登记已经释放。"""

    class _FailingAssistantSessionManager(_SessionManager):
        async def add_message(self, *args, **kwargs) -> int:
            if kwargs.get("role") == "assistant":
                raise RuntimeError("save failed")
            return await super().add_message(*args, **kwargs)

    sessions = _FailingAssistantSessionManager({"id": "s1", "user_id": "user_x", "system_prompt": "sys"})
    context = _ContextManager()
    tasks = TaskService()
    service = ChatService(
        session_manager=sessions,
        context_manager=context,
        task_service=tasks,
        llm=object(),
        tools=object(),
        agent_params=agent_params,
    )
    run = await service.prepare_message(
        session_id="s1",
        user_id="user_x",
        message="question",
        max_iterations=None,
    )
    run.agent._result = AgentResult(success=True, content="answer")

    with pytest.raises(RuntimeError, match="save failed"):
        await run.aclose()

    assert tasks.get_cancel_event(run.run_id) is None
    assert [item["role"] for item in sessions.saved] == ["user"]
