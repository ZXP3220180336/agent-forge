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
from app.domain.ports.llm_gateway import StreamResult
from app.integration.tools.tool_service import ToolService
from app.integration.tools.base import BaseTool, ToolResult
from app.shared.error_handling import (
    AgentRunError,
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    ErrorHandlerRegistry,
)
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


class _EmptyLLM:
    """每轮返回空输出（finish_reason 为空），用于触发重试 / 空输出上限硬终止。"""

    async def async_generate(self, *args, result=None, **kwargs):
        if result is not None:
            result.finish_reason = ""
            result.content = ""
        yield ""
        return


class _ScriptedLLM:
    """按脚本返回 StreamResult 字段的 LLM 替身（脚本耗尽复用最后一条）。"""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = 0

    async def async_generate(self, *args, result=None, **kwargs):
        self.calls += 1
        spec = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield ""
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
async def test_react_agent_passes_cost_limiter_to_strategy():
    """ReActAgent 构造注入的 cost_limiter 透传到内部 ReActStrategy。"""
    from app.application.context.cost_limiter import CostLimiter
    from app.integration.llm.cost_tracker import CostTracker

    class _Calc:
        @staticmethod
        def calculate_cost(usage, model=""):
            return CostTracker.calculate(usage, model)

    llm = _NoopLLM()
    cl = CostLimiter(ceiling=0.5, llm=_Calc(), model="gpt-4o-mini")
    agent = ReActAgent(llm=llm, tools=None, cost_limiter=cl)

    assert agent._strategy._cost_limiter is cl


@pytest.mark.asyncio
async def test_react_agent_passes_max_empty_retries_to_strategy():
    """ReActAgent 经 AgentContext.max_empty_retries 透传给 execute（空输出超限硬终止）。"""
    agent = ReActAgent(llm=_EmptyLLM(), tools=None)
    ctx = AgentContext(session_id="s", user_id="u", max_iterations=6, max_empty_retries=1)

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    # max_empty_retries=1：1 次重试 + 第 2 次空输出终止（iterations=2）
    assert agent.result is not None
    assert agent.result.success is False
    assert agent.result.iterations == 2
    assert "连续空输出" in (agent.result.error or "")


@pytest.mark.asyncio
async def test_react_agent_passes_max_llm_fail_retries_to_strategy():
    """ReActAgent 经 AgentContext.max_llm_fail_retries 透传给 execute（失败重试上限硬终止）。"""
    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    agent = ReActAgent(llm=_ErrorLLM(), tools=None, error_handlers=registry)
    ctx = AgentContext(
        session_id="s", user_id="u", max_iterations=6, max_llm_fail_retries=0
    )

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    # max_llm_fail_retries=0：首次失败即终止（即便 handler CONTINUE）；透传失效则
    # 默认 2 → 第 3 次失败才终止
    assert agent.result is not None
    assert agent.result.success is False
    assert agent.result.iterations == 1
    assert "连续 LLM 调用失败" in (agent.result.error or "")


@pytest.mark.asyncio
async def test_react_agent_passes_max_same_action_turns_to_strategy():
    """ReActAgent 经 AgentContext.max_same_action_turns 透传给 execute（停滞超限终止）。"""
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "tool_a", "arguments": json.dumps({"query": "x"})},
    }
    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [call]}] * 2)
    reg = ToolService(max_concurrent_tools=10)
    reg.register(_DelayTool("tool_a", delay=0.001))
    agent = ReActAgent(llm=llm, tools=reg)
    ctx = AgentContext(session_id="s", user_id="u", max_iterations=5, max_same_action_turns=1)

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    # max_same_action_turns=1：第 2 轮相同工具调用终止（iterations=2）
    assert agent.result is not None
    assert agent.result.success is False
    assert agent.result.iterations == 2
    assert "相同工具调用" in (agent.result.error or "")


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


class _ThrowingLLM:
    """LLM 抛未捕获异常（触发 run 的 UNKNOWN 错误分发）。"""

    async def async_generate(self, *args, **kwargs):
        yield ""  # 使成为 async generator（否则被视为 async 函数返回 coroutine）
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_unknown_handler_raise_propagates():
    """自定义 UNKNOWN handler → RAISE：抛 AgentRunError(kind=UNKNOWN) 给调用方。

    主循环未捕获异常 → UNKNOWN 分发（与其余 kind 统一 RAISE 语义，抛 AgentRunError
    而非 re-raise 原始异常）；BaseAgent.run 对 AgentRunError 不吞、上抛。
    """
    async def on_unknown(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.UNKNOWN, on_unknown)

    agent = ReActAgent(
        llm=_ThrowingLLM(), tools=None, error_handlers=registry
    )
    ctx = AgentContext(session_id="s", user_id="u", max_iterations=3)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in agent.run(
            "hi", [{"role": "user", "content": "hi"}], ctx
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.UNKNOWN
    assert "Agent 运行异常" in exc_info.value.message


@pytest.mark.asyncio
async def test_agent_error_reraisd_not_swallowed():
    """策略内 RAISE 抛出的 AgentRunError → run 不吞，上抛给调用方。"""
    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    agent = ReActAgent(
        llm=_ErrorLLM(), tools=None, error_handlers=registry
    )
    ctx = AgentContext(session_id="s", user_id="u", max_iterations=3)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in agent.run(
            "hi", [{"role": "user", "content": "hi"}], ctx
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.LLM_FAILED


class _GenerateOnlyLLM:
    """只实现 generate()（非流式通道）、不实现 async_generate——stream_mode 透传哨兵：

    ReActAgent 若未把 ctx.stream_mode=False 透传给策略，run() 会调 async_generate
    而本替身无此方法 → AttributeError，测试即失败。
    """

    async def generate(
        self,
        messages=None,
        tools=None,
        temperature=None,
        max_tokens=None,
        model_key=None,
        response_format=None,
    ) -> StreamResult:
        sr = StreamResult()
        sr.finish_reason = "stop"
        sr.content = "答案"
        return sr


async def test_react_agent_passes_stream_mode_to_strategy():
    """ctx.stream_mode=False → ReActAgent 透传 → 非流式通道生效（哨兵假 LLM 证明）。"""
    agent = ReActAgent(llm=_GenerateOnlyLLM(), tools=None)
    ctx = AgentContext(session_id="s", user_id="u", max_iterations=3, stream_mode=False)

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    assert agent.result is not None
    assert agent.result.success is True
    assert agent.result.content == "答案"
