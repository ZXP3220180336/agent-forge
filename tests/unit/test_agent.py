"""
ReActAgent 单元测试

覆盖：
    ReActAgent 生命周期、上下文参数映射与 ReActOutcome → AgentResult 桥接
"""

import asyncio
import json
import time

import pytest

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


def _ctx(**overrides) -> AgentContext:
    values = {
        "session_id": "s",
        "user_id": "u",
        "run_id": "run-test",
        "run_stop": asyncio.Event(),
    }
    values.update(overrides)
    return AgentContext(**values)


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"run_id": " "}, ValueError),
        ({"run_stop": object()}, TypeError),
        ({"parent_cancel_events": [asyncio.Event()]}, TypeError),
        ({"workflow_id": " "}, ValueError),
    ],
)
@pytest.mark.asyncio
async def test_agent_rejects_invalid_run_control_context(overrides, error_type):
    agent = ReActAgent(llm=_NoopLLM(), tools=ToolService())
    with pytest.raises(error_type):
        async for _ in agent.run("x", [], _ctx(**overrides)):
            pass


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
    """最小 LLM 替身。"""

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
    ctx = _ctx(max_iterations=6, max_empty_retries=1)

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
    ctx = _ctx(max_iterations=6, max_llm_fail_retries=0)

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    # max_llm_fail_retries=0：首次失败即终止（即便 handler CONTINUE）；透传失效则
    # 默认 2 → 第 3 次失败才终止
    assert agent.result is not None
    assert agent.result.success is False
    assert agent.result.iterations == 1
    assert "连续 LLM 调用失败" in (agent.result.error or "")


@pytest.mark.asyncio
async def test_react_agent_passes_max_tool_protocol_retries_to_strategy():
    """AgentContext 的协议修正上限必须进入 ReActStrategy。"""
    llm = _ScriptedLLM([{"finish_reason": "tool_calls"}])
    agent = ReActAgent(llm=llm, tools=None)
    ctx = _ctx(
        max_iterations=6,
        max_tool_protocol_retries=0,
    )

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    assert llm.calls == 1
    assert agent.result is not None
    assert "连续工具调用协议异常" in (agent.result.error or "")


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
    ctx = _ctx(max_iterations=5, max_same_action_turns=1)

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    # max_same_action_turns=1：第 2 轮相同工具调用终止（iterations=2）
    assert agent.result is not None
    assert agent.result.success is False
    assert agent.result.iterations == 2
    assert "相同工具调用" in (agent.result.error or "")


@pytest.mark.asyncio
async def test_strategy_cycle_short_circuits_on_llm_error():
    """LLM 失败（StreamResult.error）→ 第 1 轮短路返回失败结果，不空转重试（LLM-001）。

    修复前：LLM create 失败被转成 SSE error 事件后 Agent 不解析事件类型，
    把「失败」当「空输出」空转到 max_iterations，最终错误信息不准确。
    修复后：Agent 检查 stream_result.error，非空即短路返回失败 AgentResult。
    """
    llm = _ErrorLLM()
    agent = ReActAgent(llm=llm, tools=None)
    ctx = _ctx(max_iterations=3)

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
    ctx = _ctx(max_iterations=3)

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
    ctx = _ctx(max_iterations=3)

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
        cancel_event=None,
        deadline=None,
    ) -> StreamResult:
        sr = StreamResult()
        sr.finish_reason = "stop"
        sr.content = "答案"
        return sr


async def test_react_agent_passes_stream_mode_to_strategy():
    """ctx.stream_mode=False → ReActAgent 透传 → 非流式通道生效（哨兵假 LLM 证明）。"""
    agent = ReActAgent(llm=_GenerateOnlyLLM(), tools=None)
    ctx = _ctx(max_iterations=3, stream_mode=False)

    async for _ in agent.run("hi", [{"role": "user", "content": "hi"}], ctx):
        pass

    assert agent.result is not None
    assert agent.result.success is True
    assert agent.result.content == "答案"
