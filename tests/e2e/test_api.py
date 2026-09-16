"""
HTTP 层 e2e 测试：路由 + DI + 请求/响应 schema

用 TestClient 不触发 lifespan（避免 container.initialize() 连真实 Redis），
monkeypatch container 单例的服务为 fake，验证真实 HTTP 路由层。
不依赖 Redis/DB/网络。
"""

import json

from fastapi.testclient import TestClient

from app.application.chat import ChatService
from app.application.context.context_manager import ContextManager
from app.application.task.task_service import TaskService
from app.container import container
from app.domain.ports.llm_gateway import StreamResult
from app.integration.llm.token_counter import TiktokenTokenCounter
from app.integration.tools.tool_service import ToolService
from app.main import app

client = TestClient(app)

AUTH = {"Authorization": "testtoken123"}  # get_current_user → "user_testtoke"


class FakeSessionManager:
    """固定会话 + 记录保存的消息 + 可定制 create_session"""

    def __init__(self, session: dict | None):
        self._session = session
        self.saved_messages: list[dict] = []

    async def get_session(self, session_id):
        return self._session

    async def get_messages(
        self,
        session_id,
        limit=50,
        offset=0,
        before_message_id=None,
    ):
        return []

    async def add_message(self, session_id, role, content, reasoning_content=None, token_count=0):
        self.saved_messages.append(
            {"session_id": session_id, "role": role, "content": content}
        )
        return len(self.saved_messages)

    async def create_session(self, user_id, system_prompt=None, title=None):
        return {
            "id": "new-session-id",
            "user_id": user_id,
            "system_prompt": system_prompt or "你是一个友好的AI助手",
            "title": title or "新对话",
            "created_at": "2026-08-15T00:00:00+00:00",
            "message_count": 0,
            "total_tokens": 0,
        }


class FakeLLM:
    """async generator：脚本返回最终答复（复用 test_chat_flow 模式）"""

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self.calls = 0

    async def async_generate(
        self,
        messages,
        tools=None,
        temperature=0.2,
        max_tokens=4096,
        result=None,
        model_key="main",
        cancel_event=None,
        deadline=None,
    ):
        self.calls += 1
        if result is None:
            result = StreamResult()
        outcome = self._script.pop(0) if self._script else {"type": "stop", "content": ""}
        result.content = outcome["content"]
        result.finish_reason = "stop"
        yield (
            f"data: {json.dumps({'type': 'message', 'content': outcome['content']}, ensure_ascii=False)}\n\n"
        )


def _wire(monkeypatch, agent_params, session, llm_script=None):
    """把 container 单例服务替换为 fake。"""
    fake_sm = FakeSessionManager(session)
    context_manager = ContextManager(fake_sm, TiktokenTokenCounter("gpt-4"))
    llm = FakeLLM(llm_script or [])
    tools = ToolService()
    tasks = TaskService()
    monkeypatch.setattr(container, "session_manager", fake_sm)
    monkeypatch.setattr(container, "context_manager", context_manager)
    monkeypatch.setattr(container, "llm_service", llm)
    monkeypatch.setattr(container, "tool_service", tools)
    monkeypatch.setattr(container, "task_service", tasks)
    monkeypatch.setattr(container, "agent_params", agent_params)
    monkeypatch.setattr(
        container,
        "chat_service",
        ChatService(
            session_manager=fake_sm,
            context_manager=context_manager,
            task_service=tasks,
            llm=llm,
            tools=tools,
            agent_params=agent_params,
        ),
    )
    return fake_sm


def test_chat_send_requires_auth():
    """缺少 Authorization 头返回 401"""
    resp = client.post("/api/chat/send", json={"session_id": "s1", "message": "hi"})
    assert resp.status_code == 401


def test_chat_send_session_not_found(monkeypatch, agent_params):
    """会话不存在返回 404"""
    fake_sm = _wire(monkeypatch, agent_params, session=None)
    resp = client.post(
        "/api/chat/send",
        json={"session_id": "s1", "message": "hi"},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
    assert resp.json()["message"] == "会话不存在"
    assert fake_sm.saved_messages == []
    assert container.llm_service.calls == 0
    assert container.task_service.cancel_session("s1") is False


def test_chat_send_forbidden_has_no_side_effects(monkeypatch, agent_params):
    """越权在响应头前返回 403，且不写消息、不登记运行、不调用 LLM。"""
    fake_sm = _wire(
        monkeypatch,
        agent_params,
        session={"id": "s1", "user_id": "another-user", "system_prompt": "sys"},
    )

    resp = client.post(
        "/api/chat/send",
        json={"session_id": "s1", "message": "hi"},
        headers=AUTH,
    )

    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"
    assert fake_sm.saved_messages == []
    assert container.llm_service.calls == 0
    assert container.task_service.cancel_session("s1") is False


def test_chat_send_streams_sse_and_saves_messages(monkeypatch, agent_params):
    """完整聊天闭环经 HTTP 层：SSE 帧 + user/assistant 消息保存"""
    fake_sm = _wire(
        monkeypatch,
        agent_params,
        session={"id": "s1", "user_id": "user_testtoke", "system_prompt": "sys"},
        llm_script=[{"type": "stop", "content": "你好，我是AI"}],
    )
    resp = client.post(
        "/api/chat/send",
        json={"session_id": "s1", "message": "你好"},
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "data:" in body
    assert "[DONE]" in body
    assert "你好，我是AI" in body

    roles = [m["role"] for m in fake_sm.saved_messages]
    assert "user" in roles
    assert "assistant" in roles
    assistant = next(m for m in fake_sm.saved_messages if m["role"] == "assistant")
    assert assistant["content"] == "你好，我是AI"


def test_chat_stop_uses_chat_service_and_cancels_session_runs(
    monkeypatch,
    agent_params,
):
    """停止端点经 ChatService 校验会话，并保持会话级取消语义。"""
    _wire(
        monkeypatch,
        agent_params,
        session={"id": "s1", "user_id": "user_testtoke", "system_prompt": "sys"},
    )
    event = container.task_service.create_cancel_event("s1", "run-1")

    resp = client.post("/api/chat/stop?session_id=s1", headers=AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"message": "已发送停止信号", "cancelled": True}
    assert event.is_set()
    container.task_service.clear_cancel_event("run-1")


def test_create_session_endpoint(monkeypatch, agent_params):
    """POST /api/session/create 经路由与 schema 返回响应"""
    fake_sm = _wire(monkeypatch, agent_params, session=None)
    resp = client.post(
        "/api/session/create",
        json={"system_prompt": "p", "title": "t"},
        headers=AUTH,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "new-session-id"
    assert data["title"] == "t"
    assert data["created_at"] == "2026-08-15T00:00:00+00:00"


def test_create_session_requires_auth():
    resp = client.post("/api/session/create", json={})
    assert resp.status_code == 401
