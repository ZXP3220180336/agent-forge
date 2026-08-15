"""
HTTP 层 e2e 测试：路由 + DI + 请求/响应 schema

用 TestClient 不触发 lifespan（避免 container.initialize() 连真实 Redis），
monkeypatch container 单例的服务为 fake，验证真实 HTTP 路由层。
不依赖 Redis/DB/网络。
"""

import json

from fastapi.testclient import TestClient

from app.application.context.context_manager import ContextManager
from app.application.task.task_service import TaskService
from app.container import container
from app.domain.ports.llm_gateway import StreamResult
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

    async def get_messages(self, session_id, limit=50, offset=0):
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

    async def async_generate(
        self,
        messages,
        tools=None,
        temperature=0.2,
        max_tokens=4096,
        result=None,
        model_key="main",
        cancel_event=None,
    ):
        if result is None:
            result = StreamResult()
        outcome = self._script.pop(0) if self._script else {"type": "stop", "content": ""}
        result.content = outcome["content"]
        result.finish_reason = "stop"
        yield (
            f"data: {json.dumps({'type': 'message', 'content': outcome['content']}, ensure_ascii=False)}\n\n"
        )


def _wire(monkeypatch, session, llm_script=None):
    """把 container 单例服务替换为 fake。"""
    fake_sm = FakeSessionManager(session)
    monkeypatch.setattr(container, "session_manager", fake_sm)
    monkeypatch.setattr(container, "context_manager", ContextManager(fake_sm))
    monkeypatch.setattr(container, "llm_service", FakeLLM(llm_script or []))
    monkeypatch.setattr(container, "tool_service", ToolService())
    monkeypatch.setattr(container, "task_service", TaskService())
    monkeypatch.setattr(
        container,
        "agent_params",
        {"max_iterations": 5, "temperature": 0.2, "max_tokens": 4096},
    )
    return fake_sm


def test_chat_send_requires_auth():
    """缺少 Authorization 头返回 401"""
    resp = client.post("/api/chat/send", json={"session_id": "s1", "message": "hi"})
    assert resp.status_code == 401


def test_chat_send_session_not_found(monkeypatch):
    """会话不存在返回 404"""
    _wire(monkeypatch, session=None)
    resp = client.post(
        "/api/chat/send",
        json={"session_id": "s1", "message": "hi"},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "会话不存在"


def test_chat_send_streams_sse_and_saves_messages(monkeypatch):
    """完整聊天闭环经 HTTP 层：SSE 帧 + user/assistant 消息保存"""
    fake_sm = _wire(
        monkeypatch,
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


def test_create_session_endpoint(monkeypatch):
    """POST /api/session/create 经路由与 schema 返回响应"""
    fake_sm = _wire(monkeypatch, session=None)
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
