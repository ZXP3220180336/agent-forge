"""
chat_router → ReActAgent 桥接集成测试

验证 /api/chat/send 走完整 ReAct 闭环：
    用户输入 → LLM 思考 → 工具调用 → 工具执行 → LLM 总结 → 回复用户

不依赖外部 API / 数据库：用 Fake LLM 编排"首轮调工具、次轮给最终答复"，
真实 ToolRegistry + WriteFileTool 验证工具真实执行。

用法：直接调用 send_message()（手动传入依赖），消费 StreamingResponse.body_iterator。
"""

import json

import pytest

from app.api.routes.chat import SendMessageRequest, send_message
from app.services.context_manager import ContextManager
from app.services.llm_service import StreamResult
from app.services.task_service import TaskService
from app.tools.builtin import WriteFileTool
from app.tools.registry import ToolRegistry


class FakeSessionManager:
    """Fake 会话管理器：固定会话 + 记录保存的消息"""

    def __init__(self, session: dict):
        self._session = session
        self.saved_messages: list[dict] = []

    async def get_session(self, session_id: str) -> dict | None:
        return self._session

    async def get_messages(self, session_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        return []  # 无历史，模拟新会话

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        reasoning_content: str | None = None,
        token_count: int = 0,
    ) -> int:
        self.saved_messages.append(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "reasoning_content": reasoning_content,
                "token_count": token_count,
            }
        )
        return len(self.saved_messages)


class FakeLLM:
    """
    Fake LLM：按脚本顺序返回结果。

    脚本项：
        {"type": "tool_calls", "tool": str, "args": dict}
        {"type": "stop", "content": str}
    """

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self.calls = 0
        self.requests: list[list[dict]] = []  # 每次调用收到的 messages 副本

    async def async_generate(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        result: StreamResult | None = None,
        model_key: str = "main",
        cancel_event=None,
    ):
        self.calls += 1
        self.requests.append(json.loads(json.dumps(messages)))  # 深拷贝
        if result is None:
            result = StreamResult()

        outcome = self._script.pop(0) if self._script else {"type": "stop", "content": ""}

        if outcome["type"] == "tool_calls":
            result.finish_reason = "tool_calls"
            result.tool_calls = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": outcome["tool"],
                        "arguments": json.dumps(outcome["args"]),
                    },
                }
            ]
            yield "data: {\"type\": \"reasoning\", \"content\": \"需要调用工具\"}\n\n"
        else:
            result.content = outcome["content"]
            result.finish_reason = "stop"
            yield f"data: {json.dumps({'type': 'message', 'content': outcome['content']}, ensure_ascii=False)}\n\n"


def _parse_sse(chunks: list[str]) -> list[dict]:
    """解析 SSE 块为事件字典列表（[DONE] 帧标记为 {"type": "DONE_FRAME"}）"""
    events: list[dict] = []
    for chunk in chunks:
        if not chunk.startswith("data: "):
            continue
        payload = chunk[6:].strip()
        if payload == "[DONE]":
            events.append({"type": "DONE_FRAME"})
        else:
            events.append(json.loads(payload))
    return events


@pytest.mark.asyncio
async def test_chat_send_message_react_loop(tmp_path):
    """验证完整 ReAct 闭环：LLM 调工具 → 工具执行 → LLM 总结 → 消息保存"""
    # 1. 准备依赖
    fake_sm = FakeSessionManager(
        {"id": "s1", "user_id": "user_x", "system_prompt": "你是一个友好的AI助手"}
    )
    context_manager = ContextManager(session_manager=fake_sm)

    target_file = tmp_path / "out.txt"
    fake_llm = FakeLLM(
        [
            {"type": "tool_calls", "tool": "writeFile", "args": {"file_path": str(target_file), "content": "你好"}},
            {"type": "stop", "content": "文件已写入"},
        ]
    )

    registry = ToolRegistry()
    registry.register(WriteFileTool())

    # 2. 调用 send_message（手动传入依赖）
    request = SendMessageRequest(session_id="s1", message="帮我写个文件", max_iterations=5)
    response = await send_message(
        request=request,
        user_id="user_x",
        session_manager=fake_sm,
        context_manager=context_manager,
        llm_service=fake_llm,
        tool_registry=registry,
        task_service=TaskService(),
    )

    # 3. 消费 SSE 流
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    events = _parse_sse(chunks)
    types = [e["type"] for e in events]

    # 4. 事件序列：工具调用 → 工具结果 → 完成 → [DONE]
    assert "tool_call" in types, f"缺少 tool_call 事件: {types}"
    assert "tool_result" in types, f"缺少 tool_result 事件: {types}"
    assert "done" in types, f"缺少 done 事件: {types}"
    assert events[-1]["type"] == "DONE_FRAME", f"末帧应为 [DONE]: {types[-1]}"

    tool_call = next(e for e in events if e["type"] == "tool_call")
    assert tool_call["content"] == "writeFile"

    # 5. 工具真实执行
    assert target_file.exists(), "writeFile 工具未实际执行"
    assert target_file.read_text(encoding="utf-8") == "你好"

    # 6. 消息保存：user 消息（发送时）+ assistant 回复（流结束后）
    roles = [m["role"] for m in fake_sm.saved_messages]
    assert "user" in roles, f"缺少 user 消息: {roles}"
    assert "assistant" in roles, f"缺少 assistant 消息: {roles}"

    assistant = next(m for m in fake_sm.saved_messages if m["role"] == "assistant")
    assert assistant["content"] == "文件已写入"
    assert assistant["session_id"] == "s1"

    # 7. ReAct 循环：首轮调工具 + 次轮给答复
    assert fake_llm.calls == 2

    # 8. 回归防护：tool 消息必须与前置 assistant 消息的 tool_calls 配对
    #    （OpenAI 兼容 API 硬性要求，缺失时下一轮请求 400）
    req2 = fake_llm.requests[1]
    roles2 = [m["role"] for m in req2]
    assert "assistant" in roles2 and "tool" in roles2, f"第二轮缺少 assistant/tool 消息: {roles2}"

    assistant_msg = next(m for m in req2 if m["role"] == "assistant")
    assert "tool_calls" in assistant_msg, "assistant 消息必须携带 tool_calls 字段"
    tool_msg = next(m for m in req2 if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == assistant_msg["tool_calls"][0]["id"], "tool_call_id 必须与 assistant.tool_calls 配对"


@pytest.mark.asyncio
async def test_chat_send_message_no_tools_plain_answer():
    """LLM 直接给答复（不调工具）时，闭环仍正常"""
    fake_sm = FakeSessionManager(
        {"id": "s2", "user_id": "user_x", "system_prompt": "你是一个友好的AI助手"}
    )
    context_manager = ContextManager(session_manager=fake_sm)

    fake_llm = FakeLLM([{"type": "stop", "content": "直接回答"}])
    registry = ToolRegistry()  # 空注册中心：无工具定义，LLM 只能直接回答

    request = SendMessageRequest(session_id="s2", message="你好", max_iterations=5)
    response = await send_message(
        request=request,
        user_id="user_x",
        session_manager=fake_sm,
        context_manager=context_manager,
        llm_service=fake_llm,
        tool_registry=registry,
        task_service=TaskService(),
    )

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    events = _parse_sse(chunks)
    types = [e["type"] for e in events]

    assert "done" in types
    assert events[-1]["type"] == "DONE_FRAME"
    assert "tool_call" not in types  # 无工具调用

    assistant = next(m for m in fake_sm.saved_messages if m["role"] == "assistant")
    assert assistant["content"] == "直接回答"
    assert fake_llm.calls == 1
