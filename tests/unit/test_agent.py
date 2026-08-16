"""
ReActAgent 单元测试

覆盖：
    _execute_tool_calls 并行执行：tool_messages 顺序保持（= tool_calls 输入顺序）
    tool_call_id 配对：tool 消息与 assistant.tool_calls 一一对应
    gather 并行：多工具同时执行（并发度由 ToolService 信号量限制）
"""

import asyncio
import json
import time

import pytest

from app.config import settings
from app.domain.agent import AgentContext, ReActAgent
from app.integration.tools.tool_service import ToolService
from app.integration.tools.base import BaseTool, ToolResult
from app.shared.events import build_error_event


class _DelayTool(BaseTool):
    """带不同延迟的工具，用于验证 gather 并行 + 顺序保持。"""

    def __init__(self, name: str, delay: float):
        self._name = name
        self.delay = delay
        self.exec_started = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "测试工具"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        self.exec_started.append(time.monotonic())
        await asyncio.sleep(self.delay)
        return ToolResult(success=True, content=f"{self._name}:{kwargs.get('query', '')}")


class _NoopLLM:
    """最小 LLM 替身（_execute_tool_calls 不真正调用 LLM）。"""

    async def async_generate(self, *args, **kwargs):
        yield ""
        return


class _ErrorLLM:
    """模拟 LLM 失败：产出一个 SSE error 事件，并在 result 上标记 error（LLM-001）。"""

    async def async_generate(self, *args, result=None, **kwargs):
        if result is not None:
            result.error = "401 认证失败"
        yield build_error_event("LLM 调用失败: 401 认证失败")
        return


@pytest.mark.asyncio
async def test_execute_tool_calls_parallel_preserves_order(monkeypatch):
    """并行执行工具：tool_messages 顺序保持 = tool_calls 输入顺序。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 10)
    reg = ToolService(max_concurrent_tools=10)
    # 工具延迟故意交错：tool2 先完成，但结果顺序仍应 = 输入顺序
    t1 = _DelayTool("tool_a", delay=0.03)
    t2 = _DelayTool("tool_b", delay=0.01)
    t3 = _DelayTool("tool_c", delay=0.02)
    for t in (t1, t2, t3):
        reg.register(t)

    llm = _NoopLLM()
    agent = ReActAgent(llm=llm, tools=reg)

    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "tool_a", "arguments": json.dumps({"query": "x1"})}},
        {"id": "call_2", "type": "function", "function": {"name": "tool_b", "arguments": json.dumps({"query": "x2"})}},
        {"id": "call_3", "type": "function", "function": {"name": "tool_c", "arguments": json.dumps({"query": "x3"})}},
    ]
    messages = []

    # 构造 context（_execute_tool_calls 需要 self._context）
    agent._context = AgentContext(session_id="s", user_id="u")
    async for _ in agent._execute_tool_calls(tool_calls, messages, iteration=1):
        pass

    # tool_messages 顺序 = 输入顺序
    assert [m["tool_call_id"] for m in messages] == ["call_1", "call_2", "call_3"]
    # 内容对应正确工具
    assert messages[0]["content"] == "tool_a:x1"
    assert messages[1]["content"] == "tool_b:x2"
    assert messages[2]["content"] == "tool_c:x3"


@pytest.mark.asyncio
async def test_execute_tool_calls_parallel_actually_concurrent(monkeypatch):
    """并行执行：总耗时小于串行和（工具延迟交错）。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 10)
    reg = ToolService(max_concurrent_tools=10)
    t1 = _DelayTool("tool_a", delay=0.05)
    t2 = _DelayTool("tool_b", delay=0.05)
    reg.register(t1)
    reg.register(t2)

    llm = _NoopLLM()
    agent = ReActAgent(llm=llm, tools=reg)
    agent._context = AgentContext(session_id="s", user_id="u")

    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
        {"id": "call_2", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}},
    ]
    messages = []

    start = time.monotonic()
    async for _ in agent._execute_tool_calls(tool_calls, messages, iteration=1):
        pass
    elapsed = time.monotonic() - start

    # 并行执行两个 0.05s 工具，总耗时应 < 串行 0.1s（留余量，断言 < 0.09）
    assert elapsed < 0.09, f"应并行执行（<0.09s），实际 {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_strategy_cycle_short_circuits_on_llm_error():
    """LLM 失败（StreamResult.error）→ 第 1 轮短路返回失败结果，不空转重试（LLM-001）。

    修复前：LLM create 失败被转成 SSE error 事件后 Agent 不解析事件类型，
    把「失败」当「空输出」空转到 max_iterations，最终错误信息不准确。
    修复后：Agent 检查 stream_result.error，非空即短路返回失败 AgentResult。
    """
    llm = _ErrorLLM()
    agent = ReActAgent(llm=llm, tools=None)
    ctx = AgentContext(session_id="s", user_id="u", max_iterations=3)

    events = []
    async for ev in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        events.append(ev)

    assert agent.result is not None
    assert agent.result.success is False, "LLM 失败应返回失败结果"
    assert agent.result.error == "401 认证失败", (
        f"应携带真实错误原因，实际: {agent.result.error}"
    )
    assert agent.result.iterations == 1, "应在第 1 轮短路，不空转重试"
    assert any('"type": "done"' in e for e in events), "应产出 done 事件"
