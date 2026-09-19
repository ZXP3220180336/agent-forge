"""
chat_router → ReActAgent 桥接集成测试

验证 /api/chat/send 走完整 ReAct 闭环：
    用户输入 → LLM 思考 → 工具调用 → 工具执行 → LLM 总结 → 回复用户

不依赖外部 API / 数据库：用 Fake LLM 编排"首轮调工具、次轮给最终答复"，
真实 ToolService + ReadFileTool 验证当前获准的只读工具真实执行。

用法：装配 ChatService 后直接调用 send_message()，消费 StreamingResponse.body_iterator。
"""

import asyncio
import json
from typing import cast

import pytest
from fastapi import Request
from starlette.requests import ClientDisconnect

from app.api.routes.chat import SendMessageRequest, send_message
from app.application.chat import ChatService
from app.application.context.context_manager import ContextManager
from app.application.task.task_service import TaskService
from app.domain.ports.llm_gateway import StreamResult
from app.integration.llm.token_counter import TiktokenTokenCounter
from app.integration.tools.builtin import ReadFileTool
from app.integration.tools.tool_service import ToolService


class _FakeRawRequest:
    """send_message 直调用桩：is_disconnected 可控（disconnect_after 次检查后为 True）。"""

    def __init__(self, disconnect_after: int = -1):
        self._calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return 0 <= self._disconnect_after < self._calls


class FakeSessionManager:
    """Fake 会话管理器：固定会话 + 记录保存的消息"""

    def __init__(self, session: dict):
        self._session = session
        self.saved_messages: list[dict] = []

    async def get_session(self, session_id: str) -> dict | None:
        return self._session

    async def get_messages(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
        before_message_id: int | None = None,
    ) -> list[dict]:
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


class _PersistedHistorySessionManager(FakeSessionManager):
    """模拟真实持久化：刚写入的用户消息会立即出现在历史查询中。"""

    async def get_messages(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
        before_message_id: int | None = None,
    ) -> list[dict]:
        return [
            {"role": item["role"], "content": item["content"]}
            for index, item in enumerate(self.saved_messages, start=1)
            if before_message_id is None or index < before_message_id
        ][offset : offset + limit]


def _chat_service(
    *,
    session_manager: FakeSessionManager,
    context_manager: ContextManager,
    llm: FakeLLM,
    tools: ToolService,
    task_service: TaskService,
    agent_params: dict,
) -> ChatService:
    """按生产装配形状创建聊天用例。"""
    return ChatService(
        session_manager=session_manager,
        context_manager=context_manager,
        task_service=task_service,
        llm=llm,
        tools=tools,
        agent_params=agent_params,
        cost_limiter=None,
    )


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
        deadline=None,
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
            yield 'data: {"type": "reasoning", "content": "需要调用工具"}\n\n'
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
async def test_chat_send_message_react_loop(tmp_path, agent_params):
    """验证完整 ReAct 闭环：LLM 调工具 → 工具执行 → LLM 总结 → 消息保存"""
    # 1. 准备依赖
    fake_sm = FakeSessionManager({"id": "s1", "user_id": "user_x", "system_prompt": "你是一个友好的AI助手"})
    context_manager = ContextManager(session_manager=fake_sm, llm=TiktokenTokenCounter("gpt-4"))

    target_file = tmp_path / "out.txt"
    target_file.write_text("你好", encoding="utf-8")
    fake_llm = FakeLLM(
        [
            {"type": "tool_calls", "tool": "readFile", "args": {"file_path": str(target_file)}},
            {"type": "stop", "content": "文件已读取"},
        ]
    )

    registry = ToolService()
    ReadFileTool.register_config(allowed_dirs=(str(tmp_path),))
    registry.register(ReadFileTool())

    # 2. 调用 send_message（手动传入依赖）
    request = SendMessageRequest(session_id="s1", message="帮我读个文件", max_iterations=5)
    response = await send_message(
        request=request,
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=fake_llm,
            tools=registry,
            task_service=TaskService(),
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),  # 直调桩：连接保持（不触发断连）
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
    assert tool_call["content"] == "readFile"

    # 5. 工具真实执行
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert "你好" in tool_result["content"], "readFile 工具未实际返回文件内容"

    # 6. 消息保存：user 消息（发送时）+ assistant 回复（流结束后）
    roles = [m["role"] for m in fake_sm.saved_messages]
    assert "user" in roles, f"缺少 user 消息: {roles}"
    assert "assistant" in roles, f"缺少 assistant 消息: {roles}"

    assistant = next(m for m in fake_sm.saved_messages if m["role"] == "assistant")
    assert assistant["content"] == "文件已读取"
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
    assert tool_msg["tool_call_id"] == assistant_msg["tool_calls"][0]["id"], (
        "tool_call_id 必须与 assistant.tool_calls 配对"
    )


@pytest.mark.asyncio
async def test_chat_send_message_no_tools_plain_answer(agent_params):
    """LLM 直接给答复（不调工具）时，闭环仍正常"""
    fake_sm = FakeSessionManager({"id": "s2", "user_id": "user_x", "system_prompt": "你是一个友好的AI助手"})
    context_manager = ContextManager(session_manager=fake_sm, llm=TiktokenTokenCounter("gpt-4"))

    fake_llm = FakeLLM([{"type": "stop", "content": "直接回答"}])
    registry = ToolService()  # 空服务：无工具定义，LLM 只能直接回答

    request = SendMessageRequest(session_id="s2", message="你好", max_iterations=5)
    response = await send_message(
        request=request,
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=fake_llm,
            tools=registry,
            task_service=TaskService(),
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),  # 直调桩：连接保持（不触发断连）
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


@pytest.mark.asyncio
async def test_chat_current_user_message_is_not_duplicated_in_llm_context(agent_params):
    """刚持久化的当前消息不能又作为历史与当前输入各出现一次。"""
    fake_sm = _PersistedHistorySessionManager({"id": "s-current", "user_id": "user_x", "system_prompt": "sys"})
    context_manager = ContextManager(
        session_manager=fake_sm,
        llm=TiktokenTokenCounter("gpt-4"),
    )
    fake_llm = FakeLLM([{"type": "stop", "content": "回答"}])

    response = await send_message(
        request=SendMessageRequest(
            session_id="s-current",
            message="同一条当前问题",
            max_iterations=5,
        ),
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=fake_llm,
            tools=ToolService(),
            task_service=TaskService(),
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),
    )

    async for _ in response.body_iterator:
        pass

    current = [item for item in fake_llm.requests[0] if item == {"role": "user", "content": "同一条当前问题"}]
    assert len(current) == 1


@pytest.mark.asyncio
async def test_chat_stop_cancels_running_agent(agent_params):
    """/chat/stop 置位 → 运行中的 Agent 优雅取消（CANCELLED），流带取消事件结束。"""
    fake_sm = FakeSessionManager({"id": "s3", "user_id": "user_x", "system_prompt": "你是一个友好的AI助手"})
    context_manager = ContextManager(session_manager=fake_sm, llm=TiktokenTokenCounter("gpt-4"))
    ts = TaskService()

    fake_llm = FakeLLM([{"type": "stop", "content": "不会到达"}])
    registry = ToolService()  # 空服务：无工具

    request = SendMessageRequest(session_id="s3", message="你好", max_iterations=5)
    response = await send_message(
        request=request,
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=fake_llm,
            tools=registry,
            task_service=ts,
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),  # 直调桩：连接保持
    )

    # 模拟 /chat/stop：按会话置位当前全部活动 run。
    assert ts.cancel_session("s3") is True

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    events = _parse_sse(chunks)
    types = [e["type"] for e in events]

    # Agent 感知取消 → 流以取消信息 + done 正常结束（优雅，非中断）
    assert "done" in types
    assert events[-1]["type"] == "DONE_FRAME"
    # 取消后 LLM 未被调用（主循环顶部即停止）
    assert fake_llm.calls == 0
    # 注册表在流结束时清理
    assert ts.cancel_session("s3") is False


@pytest.mark.asyncio
async def test_chat_client_disconnect_auto_cancels(monkeypatch, agent_params):
    """客户端被动断连 → 自动置位会话取消（优雅停），不再发新 LLM 调用、停止向断连端推送。

    场景：LLM 首轮声明调工具（registry 空 → 协议错误会 CONTINUE 触发第二轮 LLM），
    首个事件推送后检测到断连 → 置位取消 → Agent 轮次边界收尾（calls 保持 1，不空转）。
    """
    fake_sm = FakeSessionManager({"id": "s4", "user_id": "user_x", "system_prompt": "你是一个友好的AI助手"})
    context_manager = ContextManager(session_manager=fake_sm, llm=TiktokenTokenCounter("gpt-4"))
    ts = TaskService()
    fake_llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "tool": "writeFile",
                "args": {"file_path": "/x", "content": "y"},
            }
        ]
    )
    registry = ToolService()  # 空服务：无工具 → 若无取消会继续调 LLM（第二轮）

    cancelled: list[str] = []
    orig_cancel = ts.cancel_session

    def spy_cancel(sid):
        cancelled.append(sid)
        return orig_cancel(sid)

    monkeypatch.setattr(ts, "cancel_session", spy_cancel)

    request = SendMessageRequest(session_id="s4", message="写个文件", max_iterations=5)
    response = await send_message(
        request=request,
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=fake_llm,
            tools=registry,
            task_service=ts,
            agent_params=agent_params,
        ),
        # 前 1 次检查正常、之后视为断连：模拟首个事件推送后连接断开
        http_request=cast(Request, _FakeRawRequest(disconnect_after=1)),
    )

    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    events = _parse_sse(chunks)
    types = [e["type"] for e in events]

    assert cancelled == ["s4"], "检测到断连应置位一次会话取消"
    assert fake_llm.calls == 1, "断连取消后不应再发起第二轮 LLM 调用（防空转烧钱）"
    assert "tool_call" not in types and "done" not in types, f"断连后应停止向断连客户端推送后续事件: {types}"
    assert ts.cancel_session("s4") is False, "流结束应清理该会话的活动运行"


@pytest.mark.asyncio
async def test_chat_failure_before_first_event_clears_run_and_finishes_sse(
    monkeypatch,
    agent_params,
):
    """运行登记后、首事件前失败仍输出 error/DONE，并释放自己的登记。"""
    fake_sm = FakeSessionManager({"id": "s-fail", "user_id": "user_x", "system_prompt": "sys"})
    context_manager = ContextManager(
        session_manager=fake_sm,
        llm=TiktokenTokenCounter("gpt-4"),
    )
    task_service = TaskService()

    async def fail_before_event(**kwargs):
        if False:
            yield ""
        raise RuntimeError("start failed")

    monkeypatch.setattr(task_service, "run_agent", fail_before_event)
    response = await send_message(
        request=SendMessageRequest(
            session_id="s-fail",
            message="hello",
            max_iterations=5,
        ),
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=FakeLLM([]),
            tools=ToolService(),
            task_service=task_service,
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),
    )

    chunks = [chunk async for chunk in response.body_iterator]
    events = _parse_sse(chunks)

    assert [event["type"] for event in events] == ["error", "DONE_FRAME"]
    assert task_service.cancel_session("s-fail") is False
    assert [item["role"] for item in fake_sm.saved_messages] == ["user"]


@pytest.mark.asyncio
async def test_chat_consumer_aclose_cancels_and_cleans_without_done(agent_params):
    """消费者提前关闭会同步关闭子流、清登记，且关闭路径不额外产出 DONE。"""
    fake_sm = FakeSessionManager({"id": "s-close", "user_id": "user_x", "system_prompt": "sys"})
    context_manager = ContextManager(
        session_manager=fake_sm,
        llm=TiktokenTokenCounter("gpt-4"),
    )
    task_service = TaskService()
    response = await send_message(
        request=SendMessageRequest(
            session_id="s-close",
            message="hello",
            max_iterations=5,
        ),
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=FakeLLM([{"type": "stop", "content": "answer"}]),
            tools=ToolService(),
            task_service=task_service,
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),
    )

    first = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert first != "data: [DONE]\n\n"
    assert task_service.cancel_session("s-close") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_chat_asgi_disconnect_closes_run_and_body_iterator(spec_version, monkeypatch, agent_params):
    """ASGI 两条断连分支都必须完成清理。

    2.4 由 `send()` 抛 OSError 触发（Starlette 转 `ClientDisconnect`）；2.3 由
    `receive()` 返回 http.disconnect 取消整个任务组。uvicorn 声明的是 2.3，
    Starlette 按 `spec_version >= (2, 4)` 分成两条互不覆盖的路径。

    判别力不同：2.4 的取消落在 body 生成器挂起于 `yield` 时，生成器的 finally
    不执行，只有响应调用边界的清理能兜住，因此该参数是包装器必要性的证明；2.3
    的取消由任务组投递，落点取决于生成器当时是否在 `await` 中——在 await 中时
    生成器随取消自然展开，清理会自行完成。故 2.3 参数是路径覆盖与「无悬挂运行 /
    许可可回收」的回归保护，不能单独证明边界清理必要。
    """
    fake_sm = FakeSessionManager({"id": "s-send", "user_id": "user_x", "system_prompt": "sys"})
    context_manager = ContextManager(
        session_manager=fake_sm,
        llm=TiktokenTokenCounter("gpt-4"),
    )
    task_service = TaskService(max_concurrent=1)
    original_run_agent = task_service.run_agent

    async def long_stream(**kwargs):
        """持续产出的子流：断连必须落在流中途，而不是流已自然结束之后。"""
        for index in range(200):
            await asyncio.sleep(0.005)
            yield f'data: {{"type": "message", "content": "{index}"}}\n\n'

    monkeypatch.setattr(task_service, "run_agent", long_stream)
    response = await send_message(
        request=SendMessageRequest(
            session_id="s-send",
            message="hello",
            max_iterations=5,
        ),
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=FakeLLM([{"type": "stop", "content": "answer"}]),
            tools=ToolService(),
            task_service=task_service,
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),
    )

    # 首个 body chunk 已发送后才触发断连，确保覆盖流中途而非响应头阶段
    first_chunk = asyncio.Event()

    async def receive():
        if spec_version == "2.3":
            await first_chunk.wait()
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()
            if spec_version == "2.4":
                raise OSError("client disconnected")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}}
    if spec_version == "2.4":
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
    else:
        # 取消作用域吸收自身取消，2.3 分支正常返回
        await response(scope, receive, send)

    assert task_service.cancel_session("s-send") is False

    # 恢复真实 Agent 路径：后续请求必须能重新取得被断连运行占用的并发许可
    monkeypatch.setattr(task_service, "run_agent", original_run_agent)
    follow_up = await send_message(
        request=SendMessageRequest(
            session_id="s-send",
            message="next",
            max_iterations=5,
        ),
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=FakeLLM([{"type": "stop", "content": "next answer"}]),
            tools=ToolService(),
            task_service=task_service,
            agent_params=agent_params,
        ),
        http_request=cast(Request, _FakeRawRequest()),
    )
    chunks = [chunk async for chunk in follow_up.body_iterator]
    assert _parse_sse(chunks)[-1]["type"] == "DONE_FRAME"


async def _run_with_iteration_budget(
    tmp_path,
    agent_params: dict,
    *,
    agent_max_iterations: int,
    request_max_iterations: int | None,
) -> FakeLLM:
    """按给定的装配值 / 请求值跑一次 send_message，返回 FakeLLM（用于读实际 LLM 调用次数）。

    脚本每轮都调工具、从不给最终答复，因此循环必然耗尽：LLM 调用次数 == 生效的 max_iterations。
    """
    fake_sm = FakeSessionManager({"id": "s_iter", "user_id": "user_x", "system_prompt": "sys"})
    context_manager = ContextManager(session_manager=fake_sm, llm=TiktokenTokenCounter("gpt-4"))
    for index in range(5):
        (tmp_path / f"out-{index}.txt").write_text(f"v{index}", encoding="utf-8")
    fake_llm = FakeLLM(
        [
            {
                "type": "tool_calls",
                "tool": "readFile",
                # 参数逐轮不同：避免触发相同动作停滞检测，保证耗尽路径只由迭代上限决定
                "args": {"file_path": str(tmp_path / f"out-{index}.txt")},
            }
            for index in range(5)
        ]
    )
    registry = ToolService()
    ReadFileTool.register_config(allowed_dirs=(str(tmp_path),))
    registry.register(ReadFileTool())

    request = SendMessageRequest(session_id="s_iter", message="帮我读几个文件", max_iterations=request_max_iterations)
    response = await send_message(
        request=request,
        user_id="user_x",
        chat_service=_chat_service(
            session_manager=fake_sm,
            context_manager=context_manager,
            llm=fake_llm,
            tools=registry,
            task_service=TaskService(),
            agent_params={**agent_params, "max_iterations": agent_max_iterations},
        ),
        http_request=cast(Request, _FakeRawRequest()),
    )

    async for _ in response.body_iterator:
        pass
    return fake_llm


@pytest.mark.asyncio
async def test_chat_send_request_iterations_override_agent_params(tmp_path, agent_params):
    """请求显式传入 max_iterations 时覆盖装配值（装配 1 被请求 3 覆盖）。"""
    fake_llm = await _run_with_iteration_budget(
        tmp_path, agent_params, agent_max_iterations=1, request_max_iterations=3
    )

    assert fake_llm.calls == 3, "请求显式 max_iterations 应覆盖装配值"


@pytest.mark.asyncio
async def test_chat_send_falls_back_to_agent_params_iterations(tmp_path, agent_params):
    """请求未提供 max_iterations 时采用装配值（装配 1 生效，不回落 schema 旧默认 10）。"""
    fake_llm = await _run_with_iteration_budget(
        tmp_path, agent_params, agent_max_iterations=1, request_max_iterations=None
    )

    assert fake_llm.calls == 1, "未提供 max_iterations 时应采用装配根值"
