"""
ReActStrategy 单元测试

覆盖：
    execute 工具循环 stop 结束（outcome 正确组装）
    execute LLM 失败短路（LLM-001 语义，iterations=1）
    execute 空输出重试后正常结束
    execute 最大迭代次数兜底
    execute_tool_calls 并行执行：顺序保持 + 实际并发

范式：手写假对象（不用 AsyncMock），LLM 替身回填 StreamResult + yield 事件。
"""

import asyncio
import json
import time
from dataclasses import replace

import pytest
from jsonschema import SchemaError

from app.domain.ports.llm_gateway import StreamResult
from app.domain.ports.tool_execution import ToolCleanupState, ToolEffectState, ToolExecutionState, ToolFact
from app.domain.ports.tool_gateway import ErrorCode
from app.domain.reasoning import ReActStrategy, ToolExecutionOptions
from app.domain.reasoning.react import (
    _EXECUTION_CLEANUP_GRACE_RATIO,
    _MAX_EXECUTION_CLEANUP_GRACE,
    _aborted_call_outcome,
    _build_assistant_message,
    _group_failures_by_kind,
    _resolve_deadlines,
    _terminal_result,
    _tool_protocol_error_detail,
)
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.execution import ToolEffectClass, ToolExecutionSpec
from app.integration.tools.tool_service import ToolService
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import build_error_event, build_message_event
from app.shared.exceptions import ContextWindowExceededError, LLMDeadlineExceededError, ToolDeadlineExceededError
from tests.reasoning_execution import reasoning_execution_args, reasoning_run_scope


class _EchoTool(BaseTool):
    """即时返回的工具（验证工具调用循环）。"""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回声工具"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        """测试替身仅操作内存，不产生外部副作用。"""
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content=f"echo:{kwargs.get('text', '')}")


class _FailingTool(BaseTool):
    """始终返回失败的工具（验证失败回喂与证据链记录）。"""

    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "失败工具"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        """测试替身仅操作内存，不产生外部副作用。"""
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=False, content="", error="模拟执行失败")


class _FailingToolNamed(BaseTool):
    """可配置名字与错误的失败工具（验证多失败分发聚合与仲裁）。"""

    def __init__(self, name: str, error: str = "模拟失败"):
        self._name = name
        self._error = error

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "失败工具"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        """测试替身仅操作内存，不产生外部副作用。"""
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=False, content="", error=self._error)


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

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        """测试替身仅操作内存，不产生外部副作用。"""
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs) -> ToolResult:
        self.exec_started.append(time.monotonic())
        await asyncio.sleep(self.delay)
        return ToolResult(success=True, content=f"{self._name}:{kwargs.get('query', '')}")


class _ScriptedLLM:
    """按脚本返回 StreamResult 字段的 LLM 替身（脚本耗尽则复用最后一条）。"""

    def __init__(self, scripts: list[dict]):
        self.scripts = scripts
        self.calls = 0

    async def async_generate(self, *args, result=None, **kwargs):
        self.calls += 1
        spec = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield build_message_event(spec.get("content", ""))


class _UsageCostLimiter:
    """仅在已有真实 usage 时超限，便于验证调用后护栏优先级。"""

    def check(self, usage):
        exceeded = bool(usage.get("total_tokens", 0))
        return exceeded, 1.0 if exceeded else 0.0


class _ErrorLLM:
    """模拟 LLM 失败：产出一个 SSE error 事件，并在 result 上标记 error（LLM-001）。"""

    async def async_generate(self, *args, result=None, **kwargs):
        if result is not None:
            result.error = "401 认证失败"
        yield build_error_event("LLM 调用失败: 401 认证失败")


class _EmptyLLM:
    """每轮返回空输出（finish_reason 为空），用于触发重试 / 迭代兜底。"""

    async def async_generate(self, *args, result=None, **kwargs):
        if result is not None:
            result.finish_reason = ""
            result.content = ""
        yield ""


class _RaisingLLM:
    """第 N 次调用起抛异常（模拟主循环未捕获异常，验证 UNKNOWN 部分进度保留）。"""

    def __init__(
        self,
        scripts: list[dict],
        raise_on_call: int = 1,
        exc: Exception | None = None,
    ):
        self.scripts = scripts
        self.raise_on_call = raise_on_call
        self.exc = exc or RuntimeError("意外故障")
        self.calls = 0

    async def async_generate(self, *args, result=None, **kwargs):
        self.calls += 1
        if self.calls >= self.raise_on_call:
            raise self.exc
        spec = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield build_message_event(spec.get("content", ""))


class _SleepyLLM:
    """第 N 次调用前 sleep，用于触发 max_execution_time 超时（脚本耗尽复用最后一条）。"""

    def __init__(self, scripts=None, sleep_before_call=None, delay=0.3):
        self.scripts = scripts or [{"finish_reason": "stop", "content": "完成"}]
        self.sleep_before_call = sleep_before_call  # None=不 sleep
        self.delay = delay
        self.calls = 0

    async def async_generate(self, *args, result=None, **kwargs):
        self.calls += 1
        spec = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        if self.sleep_before_call is not None and self.calls >= self.sleep_before_call:
            await asyncio.sleep(self.delay)  # 超时点在 LLM await 内（干净超时场景）
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield build_message_event(spec.get("content", ""))


def _make_registry(max_concurrent: int = 10, tools: list | None = None) -> ToolService:
    reg = ToolService(max_concurrent_tools=max_concurrent)
    for tool in tools or []:
        reg.register(tool)
    return reg


@pytest.mark.asyncio
async def test_react_execute_tool_loop_ends_on_stop():
    """工具循环：第 1 轮调工具，第 2 轮 stop → outcome 正确组装。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ],
            },
            {"finish_reason": "stop", "content": "答案是 86"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    messages = [{"role": "user", "content": "30C 转华氏"}]
    events = []
    async for ev in strategy.execute(
        "30C 转华氏", messages, **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024)
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "答案是 86"
    assert strategy.outcome.iterations == 2
    # 工具调用记录：1 条，参数与结果正确
    assert len(strategy.outcome.tool_calls) == 1
    assert strategy.outcome.tool_calls[0]["tool"] == "echo"
    assert strategy.outcome.tool_calls[0]["result"] == "echo:hi"
    # tool 消息已回喂（含 assistant.tool_calls 配对）
    assert any(m.get("role") == "tool" for m in messages)
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_execute_short_circuits_on_llm_error():
    """LLM 失败（StreamResult.error）→ 第 1 轮短路返回失败结果，不空转重试（LLM-001）。"""
    strategy = ReActStrategy(llm=_ErrorLLM(), tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.error == "401 认证失败"
    assert strategy.outcome.iterations == 1


@pytest.mark.asyncio
async def test_react_execute_empty_output_retries_then_stops():
    """空输出（finish_reason 空）→ 重试；下一轮 stop → 正常结束。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "", "content": ""},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.iterations == 2


@pytest.mark.asyncio
async def test_react_execute_max_iterations_fallback():
    """持续空输出 → 达到 max_iterations 强制结束（用最近可见结果兜底）。"""
    strategy = ReActStrategy(llm=_EmptyLLM(), tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=2, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.iterations == 2
    assert strategy.outcome.success is False


# ---------------------------------------------------------------
# 空输出重试上限（增强项 #25）：连续空输出超过 max_empty_retries → EMPTY_OUTPUT 分发硬终止
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_empty_output_retry_limit_stops():
    """持续空输出超过默认上限（2）→ 第 3 次空输出硬终止（iterations=3，error 记录）。"""
    strategy = ReActStrategy(llm=_EmptyLLM(), tools=None)

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 3  # 2 次重试 + 第 3 次硬终止
    assert "连续空输出" in (strategy.outcome.error or "")
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_empty_output_retry_limit_recovers():
    """空输出恰好 2 次（= 上限）后第 3 轮 stop → 正常结束（count > 上限才终止）。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "", "content": ""},
            {"finish_reason": "", "content": ""},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=5, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.iterations == 3


@pytest.mark.asyncio
async def test_react_empty_output_retry_limit_configurable():
    """max_empty_retries=1 → 第 2 次空输出硬终止（iterations=2）。"""
    strategy = ReActStrategy(llm=_EmptyLLM(), tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=5, temperature=0.2, max_tokens=1024, max_empty_retries=1),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 2
    assert "连续空输出" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_empty_output_count_resets_after_output():
    """空输出 → 工具调用（有产出计数清零）→ 空输出 → stop：非连续空输出不达上限。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "", "content": ""},
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ],
            },
            {"finish_reason": "", "content": ""},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
    ):
        pass

    # 空输出 count=1 → 工具调用清零 → 空输出 count=1（未超限）→ stop 正常结束
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.iterations == 4


@pytest.mark.asyncio
async def test_react_empty_output_retry_limit_handler_raise():
    """达空输出上限：硬终止分支仍先 dispatch——handler 前 2 次 CONTINUE、第 3 次 RAISE 则上抛。"""
    calls = {"n": 0}

    async def on_empty(ctx: AgentErrorContext) -> AgentErrorAction:
        calls["n"] += 1
        # 前 2 次（重试）→ CONTINUE；第 3 次（硬终止）→ RAISE
        return AgentErrorAction.RAISE if calls["n"] >= 3 else AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.EMPTY_OUTPUT, on_empty)

    strategy = ReActStrategy(llm=_EmptyLLM(), tools=None, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.EMPTY_OUTPUT
    assert "连续空输出" in exc_info.value.message  # 硬终止分支的消息
    assert calls["n"] == 3  # 2 次重试分发 + 1 次硬终止分发


# ---------------------------------------------------------------
# 循环停滞检测（增强项 #24）：相同工具+参数连续重复超过上限 → STALLED 分发硬终止
# ---------------------------------------------------------------


def _echo_call(text: str = "hi") -> dict:
    """构造 echo 工具调用（便于重复指纹测试）。"""
    return {
        "id": "call_echo",
        "type": "function",
        "function": {"name": "echo", "arguments": json.dumps({"text": text})},
    }


def _final_answer_call(call_id: str = "call_final") -> dict:
    """构造 final_answer 工具调用（注入结构化终止工具的 wire 形态）。"""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "final_answer", "arguments": "{}"},
    }


_HANG_SECONDS = 5.0


async def _hang_until_cancelled() -> None:
    """用有界等待表达「挂起」：正常路径由取消 / 超时打断，回归时以有界等待失败而非挂死测试进程。

    仓库未安装 pytest-timeout，无界等待（asyncio.Event().wait()）一旦失去取消链
    会永久占住整个测试进程，掩盖回归的真实失败面。
    """
    await asyncio.sleep(_HANG_SECONDS)


@pytest.mark.asyncio
async def test_react_tolerates_tool_call_without_function_payload():
    """网关返回缺 function 的调用时按未注册工具失败回喂，不抛 KeyError 逃逸。

    协议判据只校验 id，该调用能通过四类判据走到停滞指纹与停机组装处；两处都直接取
    `["function"]["name"]`，会抛 KeyError 把协议问题变成运行异常（UNKNOWN）。
    """
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [{"id": "call_1"}]},
            {"finish_reason": "stop", "content": "改用其他方式完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=4, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "改用其他方式完成"
    assert llm.calls == 2
    assert len(strategy.outcome.tool_calls) == 1
    assert strategy.outcome.tool_calls[0]["success"] is False


@pytest.mark.asyncio
async def test_react_stall_same_action_stops():
    """同工具同参数连续 4 轮（默认 max=3）→ 第 4 轮 STALLED 终止，该轮工具不执行。"""
    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [_echo_call()]}] * 4)
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 4
    assert "相同工具调用" in (strategy.outcome.error or "")
    assert "echo" in (strategy.outcome.error or "")
    # 第 4 轮工具未执行（停滞判定在工具执行前）：前 3 轮已执行
    assert len(strategy.outcome.tool_calls) == 3
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_stall_allows_below_limit():
    """连续 3 轮相同（= 默认上限）后 stop → 正常结束（count=3 不 > 3）。"""
    llm = _ScriptedLLM(
        [{"finish_reason": "tool_calls", "tool_calls": [_echo_call()]}] * 3
        + [{"finish_reason": "stop", "content": "完成"}]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert len(strategy.outcome.tool_calls) == 3


@pytest.mark.asyncio
async def test_react_stall_different_args_resets():
    """同工具参数变化 → 指纹不同重置，不触发停滞。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call("a")]},
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call("b")]},
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call("a")]},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert len(strategy.outcome.tool_calls) == 3


@pytest.mark.asyncio
async def test_react_stall_change_tool_resets():
    """换工具 → 指纹不同重置，不触发停滞。"""
    fail_call = {
        "id": "call_fail",
        "type": "function",
        "function": {"name": "fail", "arguments": "{}"},
    }
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "tool_calls", "tool_calls": [fail_call]},
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool(), _FailingTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert len(strategy.outcome.tool_calls) == 3


@pytest.mark.asyncio
async def test_react_stall_limit_configurable():
    """max_same_action_turns=1 → 第 2 轮相同工具调用终止（iterations=2）。"""
    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [_echo_call()]}] * 2)
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=6, temperature=0.2, max_tokens=1024, max_same_action_turns=1
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 2
    assert "相同工具调用" in (strategy.outcome.error or "")
    assert len(strategy.outcome.tool_calls) == 1  # 第 2 轮不执行


@pytest.mark.asyncio
async def test_react_stall_final_answer_not_counted():
    """final_answer 轮不参与停滞检测（指纹排除），正常终止 structured 提取。"""
    fa_call = {
        "id": "fa1",
        "type": "function",
        "function": {
            "name": "final_answer",
            "arguments": json.dumps({"conclusion": "根因A", "confidence": 0.9}),
        },
    }
    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [fa_call]}])
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=6, temperature=0.2, max_tokens=1024, output_schema=_FA_REPORT_SCHEMA
        ),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.structured == {"conclusion": "根因A", "confidence": 0.9}
    # 停滞计数未被 final_answer 污染
    assert strategy._stall_count == 0
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_stall_handler_raise():
    """达上限 + STALLED handler → RAISE：抛 AgentRunError(kind=STALLED)。"""

    async def on_stalled(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.STALLED, on_stalled)

    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [_echo_call()]}] * 4)
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.STALLED
    assert "相同工具调用" in exc_info.value.message


@pytest.mark.asyncio
async def test_react_stall_args_normalized():
    """参数 key 顺序 / 空白不同但语义相同 → 指纹一致（规范化），连续 4 轮触发停滞。"""
    call_a = {
        "id": "call_n",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"a": 1, "b": 2}'},
    }
    call_b = {
        "id": "call_n",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"b":2,"a":1}'},
    }
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [call_a]},
            {"finish_reason": "tool_calls", "tool_calls": [call_b]},
            {"finish_reason": "tool_calls", "tool_calls": [call_b]},
            {"finish_reason": "tool_calls", "tool_calls": [call_a]},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024),
    ):
        pass

    # a、b 规范化后指纹相同 → 4 轮连续相同 → 第 4 轮终止（前 3 轮执行）
    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 4
    assert len(strategy.outcome.tool_calls) == 3


# ---------------------------------------------------------------
# 模型拒答（增强项 #26）：refusal 字段 / content_filter → REFUSED 分发硬终止
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_refused_stops():
    """模型拒答（refusal 非空 + stop + 有 content）→ REFUSED 终止，不误判为成功答案。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "stop",
                "content": "抱歉，我无法回答这个问题。",
                "refusal": "内容安全策略触发，拒绝回答",
            }
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 1
    assert "模型拒答" in (strategy.outcome.error or "")
    # content 保留（拒答同时有部分输出，但不算成功）
    assert strategy.outcome.content == "抱歉，我无法回答这个问题。"
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_refused_content_filter_stops():
    """finish_reason=content_filter（无 refusal 字段）→ REFUSED 终止，不落入空输出重试。"""
    llm = _ScriptedLLM([{"finish_reason": "content_filter", "content": ""}])
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 1
    assert "模型拒答" in (strategy.outcome.error or "")
    assert "content_filter" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_refused_handler_raise():
    """REFUSED handler → RAISE：抛 AgentRunError(kind=REFUSED)。"""

    async def on_refused(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.REFUSED, on_refused)

    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "", "refusal": "拒绝"}])
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.REFUSED
    assert "模型拒答" in exc_info.value.message


@pytest.mark.asyncio
async def test_react_refused_continue_ignored():
    """REFUSED handler → CONTINUE 被忽略：终结护栏仍 STOP 组装（不重试拒答）。"""

    async def on_refused(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.REFUSED, on_refused)

    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "", "refusal": "拒绝"}])
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 1
    assert "模型拒答" in (strategy.outcome.error or "")


# ---------------------------------------------------------------
# 未捕获异常（UNKNOWN 分发）：主循环兜底，保留部分进度
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_unknown_exception_keeps_partial_progress():
    """主循环中途未捕获异常 → UNKNOWN 终止，保留已执行工具证据链。"""
    llm = _RaisingLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "stop", "content": "不会到达"},
        ],
        raise_on_call=2,  # 第 1 轮工具已执行，第 2 轮 LLM 调用抛异常
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "Agent 运行异常" in (strategy.outcome.error or "")
    # 第 1 轮工具已执行，证据链保留（修复前异常路径全部丢失）
    assert len(strategy.outcome.tool_calls) == 1
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_unknown_exception_handler_raise():
    """UNKNOWN handler → RAISE：中途异常抛 AgentRunError(kind=UNKNOWN)。"""

    async def on_unknown(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.UNKNOWN, on_unknown)

    llm = _RaisingLLM([{"finish_reason": "stop", "content": "x"}], raise_on_call=1)
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.UNKNOWN
    assert "Agent 运行异常" in exc_info.value.message


# ---------------------------------------------------------------
# 请求预算闸拒绝（CONTEXT_EXCEEDED 终结）：非 LLM_FAILED/UNKNOWN，不重试
# ---------------------------------------------------------------


# ---------------------------------------------------------------
# assistant 历史组装（纯转换）：纯空轮不组装 / thinking 回喂 / tool_calls 配对
# ---------------------------------------------------------------


def test_build_assistant_message_skips_empty_round() -> None:
    """纯空轮不组装 assistant 消息（空消息不写历史，防上下文污染）。"""
    assert _build_assistant_message(StreamResult()) is None


def test_build_assistant_message_keeps_empty_reasoning_on_thinking_signal() -> None:
    """thinking 模型返回空 reasoning 也要回喂该字段（否则下一轮 400）。"""
    result = StreamResult()
    result.has_reasoning = True

    assert _build_assistant_message(result) == {"role": "assistant", "content": "", "reasoning_content": ""}


def test_build_assistant_message_pairs_tool_calls_for_following_receipts() -> None:
    """tool_calls 必须留在 assistant 消息上，供后续 tool 回执配对。"""
    call = _echo_call()
    result = StreamResult()
    result.tool_calls = [call]

    assert _build_assistant_message(result) == {"role": "assistant", "content": "", "tool_calls": [call]}


def test_build_assistant_message_omits_reasoning_without_signal() -> None:
    """无 thinking 信号（chat 模型）不回喂 reasoning_content 字段。"""
    result = StreamResult()
    result.content = "答案"

    assert _build_assistant_message(result) == {"role": "assistant", "content": "答案"}


# ---------------------------------------------------------------
# 工具调用协议异常判据（纯函数）：四类原因与固定优先级
# ---------------------------------------------------------------


def test_tool_protocol_error_detail_returns_none_for_valid_response() -> None:
    """有工具、身份合法、未混用终止工具 → 响应有效，不做协议拦截。"""
    calls = [_echo_call()]

    assert _tool_protocol_error_detail("tool_calls", calls, has_tools=True, output_schema=None) is None


def test_tool_protocol_error_detail_accepts_lone_final_answer() -> None:
    """单独调用 final_answer 是正常终止路径，不属协议异常。"""
    calls = [_final_answer_call()]

    assert _tool_protocol_error_detail("tool_calls", calls, has_tools=True, output_schema=_FA_REPORT_SCHEMA) is None


def test_tool_protocol_error_detail_skips_non_tool_calls_finish_reason() -> None:
    """finish_reason 非 tool_calls 时不进入协议检查（正常回答 / 空输出走各自分支）。"""
    assert _tool_protocol_error_detail("stop", [], has_tools=False, output_schema=None) is None


def test_tool_protocol_error_detail_reports_missing_calls() -> None:
    detail = _tool_protocol_error_detail("tool_calls", [], has_tools=True, output_schema=None)

    assert "未返回工具调用" in detail


def test_tool_protocol_error_detail_reports_no_available_tools() -> None:
    calls = [_echo_call()]

    detail = _tool_protocol_error_detail("tool_calls", calls, has_tools=False, output_schema=None)

    assert "无可用工具" in detail


def test_tool_protocol_error_detail_reports_identity_error() -> None:
    """批内 id 重复 → 身份不可用即无法与 tool 回执配对。"""
    calls = [_echo_call(), _echo_call()]

    detail = _tool_protocol_error_detail("tool_calls", calls, has_tools=True, output_schema=None)

    assert "重复" in detail


def test_tool_protocol_error_detail_reports_final_answer_mixed_with_other_tools() -> None:
    calls = [_echo_call(), _final_answer_call()]

    detail = _tool_protocol_error_detail("tool_calls", calls, has_tools=True, output_schema=_FA_REPORT_SCHEMA)

    assert "不能与其他工具同轮调用" in detail


def test_tool_protocol_error_detail_prefers_identity_error_over_mixed_final_answer() -> None:
    """两类违规同时成立时按固定优先级归因身份异常（结构缺陷优先于用法缺陷）。"""
    calls = [_echo_call(), _final_answer_call("call_echo")]  # 与 echo 同 id → 批内重复

    detail = _tool_protocol_error_detail("tool_calls", calls, has_tools=True, output_schema=_FA_REPORT_SCHEMA)

    assert "重复" in detail


# ---------------------------------------------------------------
# deadline 派生（纯函数）：未设限 / 收尾窗口 / 上界 / 负值钳制
# ---------------------------------------------------------------


def test_resolve_deadlines_returns_none_pair_without_limit() -> None:
    assert _resolve_deadlines(None, 1000.0) == (None, None)


def test_resolve_deadlines_keeps_cleanup_window_before_hard_timeout() -> None:
    """内部 deadline 必须早于外层硬超时，给 close/settle/日志留收尾窗口。"""
    deadline, hard_timeout_at = _resolve_deadlines(30.0, 1000.0)

    assert hard_timeout_at == 1030.0
    assert deadline == hard_timeout_at - min(_MAX_EXECUTION_CLEANUP_GRACE, 30.0 * _EXECUTION_CLEANUP_GRACE_RATIO)


def test_resolve_deadlines_caps_cleanup_window_for_long_budget() -> None:
    """长时限下窗口取上界：按比例算出的值不再随总时长增长。"""
    deadline, hard_timeout_at = _resolve_deadlines(600.0, 0.0)

    assert hard_timeout_at - deadline == _MAX_EXECUTION_CLEANUP_GRACE


def test_resolve_deadlines_clamps_negative_budget_to_zero() -> None:
    """负时限按 0 处理：不产生「deadline 晚于硬超时」的倒流窗口。"""
    assert _resolve_deadlines(-5.0, 1000.0) == (1000.0, 1000.0)


# ---------------------------------------------------------------
# 未正常返回的工具结局还原（纯函数）：权威事实选取 / 回执参数降级 / 耗时
# ---------------------------------------------------------------


def _call_fact(
    *,
    attempt_id: str | None,
    revision: int,
    execution_state: ToolExecutionState,
    result: ToolResult | None = None,
) -> ToolFact:
    """构造某个 call 的事实快照（attempt_id=None 即操作事实）。"""
    return ToolFact(
        operation_id="operation-1",
        attempt_id=attempt_id,
        run_id="run-1",
        batch_id="batch-1",
        tool_call_id="call-1",
        revision=revision,
        execution_state=execution_state,
        effect_state=ToolEffectState.UNKNOWN,
        cleanup_state=ToolCleanupState.NOT_NEEDED,
        result=result,
    )


def _aborted_call(arguments: str = "{}") -> dict:
    return {"id": "call-1", "type": "function", "function": {"name": "echo", "arguments": arguments}}


def test_aborted_call_outcome_reports_unexecuted_without_any_fact() -> None:
    """任务仍挂起（无任何事实）时报未执行；回执参数仍按模型输入解析。"""
    exec_result, tool_args, elapsed = _aborted_call_outcome(_aborted_call('{"text": "hi"}'), [])

    assert exec_result.success is False
    assert "未执行" in exec_result.error
    assert exec_result.effect_state == ToolEffectState.NONE
    assert tool_args == {"text": "hi"}
    assert elapsed == 0.0


def test_aborted_call_outcome_prefers_attempt_snapshot_over_preregistered_fact() -> None:
    """预登记的 NOT_STARTED 不得盖过已完成的 attempt 快照（外部 Gateway 可能只发 attempt 事实）。"""
    confirmed = ToolResult(True, "done", execution_time=1.25)
    facts = [
        _call_fact(attempt_id=None, revision=0, execution_state=ToolExecutionState.NOT_STARTED),
        _call_fact(attempt_id="attempt-1", revision=1, execution_state=ToolExecutionState.SUCCEEDED, result=confirmed),
    ]

    exec_result, _, elapsed = _aborted_call_outcome(_aborted_call(), facts)

    # ToolFact.__post_init__ 深拷贝 result，故按值而非身份断言。
    assert exec_result == confirmed
    assert elapsed == 1.25


def test_aborted_call_outcome_prefers_operation_fact_that_has_result() -> None:
    """操作事实已带结果时直接采用，不回退到更早的 attempt 事实。"""
    operation = ToolResult(True, "operation", execution_time=2.0)
    stale_attempt = ToolResult(False, "", error="旧尝试")
    facts = [
        _call_fact(attempt_id=None, revision=2, execution_state=ToolExecutionState.SUCCEEDED, result=operation),
        _call_fact(attempt_id="attempt-1", revision=1, execution_state=ToolExecutionState.FAILED, result=stale_attempt),
    ]

    exec_result, _, elapsed = _aborted_call_outcome(_aborted_call(), facts)

    assert exec_result == operation
    assert exec_result.content == "operation"
    assert elapsed == 2.0


def test_aborted_call_outcome_takes_last_attempt_fact_when_unconfirmed() -> None:
    """attempt 事实均无结果时取最后一个，报「结果尚未确认」而非「未执行」。"""
    facts = [
        _call_fact(attempt_id="attempt-1", revision=1, execution_state=ToolExecutionState.RUNNING),
        _call_fact(attempt_id="attempt-2", revision=2, execution_state=ToolExecutionState.RUNNING),
    ]

    exec_result, _, elapsed = _aborted_call_outcome(_aborted_call(), facts)

    assert exec_result.success is False
    assert "尚未确认" in exec_result.error
    assert elapsed == 0.0


def test_aborted_call_outcome_keeps_in_flight_retry_unconfirmed() -> None:
    """重试在途时不得采信上一次尝试的旧结果。

    执行器每次 attempt 完成都会重发操作事实（revision=attempt+1），在途重试期间它带的是
    上一次尝试的结果；采信它会把「副作用未确认」报成「已确认失败」。
    """
    first_attempt = ToolResult(False, "", error="第一次尝试失败")
    facts = [
        _call_fact(attempt_id=None, revision=1, execution_state=ToolExecutionState.FAILED, result=first_attempt),
        _call_fact(
            attempt_id="attempt-1", revision=1, execution_state=ToolExecutionState.FAILED, result=first_attempt
        ),
        _call_fact(attempt_id="attempt-2", revision=0, execution_state=ToolExecutionState.RUNNING),
    ]

    exec_result, _, _ = _aborted_call_outcome(_aborted_call(), facts)

    assert exec_result.success is False
    assert "尚未确认" in exec_result.error
    assert exec_result.effect_state == ToolEffectState.UNKNOWN


def test_aborted_call_outcome_zeroes_elapsed_without_timing() -> None:
    """事实带结果但未记耗时 → 耗时按 0 回执，不把 None 传下去。"""
    facts = [
        _call_fact(
            attempt_id=None, revision=1, execution_state=ToolExecutionState.SUCCEEDED, result=ToolResult(True, "ok")
        )
    ]

    _, _, elapsed = _aborted_call_outcome(_aborted_call(), facts)

    assert elapsed == 0.0


@pytest.mark.parametrize("arguments", ["{", "[]", "null"])
def test_aborted_call_outcome_degrades_unusable_arguments_to_empty(arguments: str) -> None:
    """参数不可用（截断 JSON / 合法但非对象）按空参回执，不重复报参数错误。"""
    _, tool_args, _ = _aborted_call_outcome(_aborted_call(arguments), [])

    assert tool_args == {}


def test_aborted_call_outcome_degrades_missing_arguments_key() -> None:
    """结构缺失的调用（无 function.arguments）不抛 KeyError。"""
    _, tool_args, _ = _aborted_call_outcome({"id": "call-1"}, [])

    assert tool_args == {}


# ---------------------------------------------------------------
# 失败记录聚类（纯函数）：记录 → kind 映射单点 / 保持记录顺序
# ---------------------------------------------------------------


def _failure(error_code: str | None, *, tool: str = "echo") -> dict:
    return {"tool": tool, "error": "boom", "success": False, "error_code": error_code}


def test_group_failures_by_kind_separates_parse_and_business_failures() -> None:
    grouped = _group_failures_by_kind([_failure(ErrorCode.JSON_PARSE.value), _failure(None)])

    assert set(grouped) == {AgentErrorKind.PARSE_FAILED, AgentErrorKind.TOOL_FAILED}


def test_group_failures_by_kind_keeps_record_order_within_kind() -> None:
    """同 kind 的原因按记录顺序聚合（fail_msg 的文本顺序 = 工具返回顺序）。"""
    grouped = _group_failures_by_kind([_failure(None, tool="a"), _failure(None, tool="b")])

    assert [record["tool"] for record in grouped[AgentErrorKind.TOOL_FAILED]] == ["a", "b"]


def test_group_failures_by_kind_returns_empty_without_failures() -> None:
    """无失败 → 映射为空，PARSE_FAILED 桶不存在（协议修正计数据此清零）。"""
    assert _group_failures_by_kind([]) == {}


def test_terminal_result_ignores_unexecuted_current_tool_calls() -> None:
    """当前轮只有未执行工具调用时，终止结果应保留上一轮可见内容。"""
    previous = StreamResult()
    previous.content = "上一轮内容"
    current = StreamResult()
    current.tool_calls = [_echo_call()]

    assert _terminal_result(current, previous) is previous


async def test_post_call_cost_with_tool_calls_only_keeps_previous_visible_result():
    """当前轮仅有未执行工具调用时，调用后成本终止应保留上一轮可见成果。"""

    class _SecondUsageExceedsCost:
        def check(self, usage):
            total_tokens = usage.get("total_tokens", 0)
            return total_tokens >= 2, float(total_tokens)

    llm = _ScriptedLLM(
        [
            {
                "content": "上一轮阶段成果",
                "finish_reason": "tool_calls",
                "tool_calls": [_echo_call("first")],
                "usage": {"total_tokens": 1},
            },
            {
                "finish_reason": "tool_calls",
                "tool_calls": [_echo_call("second")],
                "usage": {"total_tokens": 1},
            },
        ]
    )
    strategy = ReActStrategy(
        llm=llm,
        tools=_make_registry(tools=[_EchoTool()]),
        cost_limiter=_SecondUsageExceedsCost(),
    )

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.content == "上一轮阶段成果"
    assert "成本超限" in (strategy.outcome.error or "")
    assert llm.calls == 2
    assert len(strategy.outcome.tool_calls) == 1, "第二轮未执行工具不能进入证据链"


@pytest.mark.parametrize("deadline_mode", ["outer_hard", "internal"])
async def test_tool_timeout_after_tool_calls_only_keeps_previous_visible_result(deadline_mode):
    """区分外层硬超时兜底和真实工具类型化 deadline，不能依赖二者调度先后。"""

    class _DeadlineBoundaryService(ToolService):
        async def execute(self, *args, **kwargs):
            if deadline_mode == "outer_hard":
                # 仅此用例隔离内部 deadline，明确命中 ReAct 外层硬 timeout。
                kwargs["call"] = replace(kwargs["call"], deadline=None)
            return await super().execute(*args, **kwargs)

    class _SecondCallSlowEcho(_EchoTool):
        def __init__(self):
            self.calls = 0

        async def execute(self, **kwargs) -> ToolResult:
            self.calls += 1
            if self.calls == 2:
                await _hang_until_cancelled()
            return await super().execute(**kwargs)

    llm = _ScriptedLLM(
        [
            {
                "content": "上一轮阶段成果",
                "finish_reason": "tool_calls",
                "tool_calls": [_echo_call("first")],
            },
            {
                "finish_reason": "tool_calls",
                "tool_calls": [_echo_call("second")],
            },
        ]
    )
    tool = _SecondCallSlowEcho()
    tools = _DeadlineBoundaryService(max_concurrent_tools=10)
    tools.register(tool)
    strategy = ReActStrategy(
        llm=llm,
        tools=tools,
    )

    async def consume():
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args(
                "react",
                max_iterations=3,
                temperature=0.2,
                max_tokens=1024,
                # 余量放大：第一轮（脚本 LLM + echo）必须在预算内跑完，否则慢机器上假失败
                max_execution_time=1.0,
            ),
        ):
            pass

    if deadline_mode == "internal":
        with pytest.raises(ToolDeadlineExceededError):
            await consume()
        assert tool.calls == 2
        assert len(strategy._tool_call_records) == 2
        assert any(f.result and f.result.content == "echo:first" for f in strategy.tool_facts)
        await tools.shutdown()
        return
    await consume()
    assert strategy.outcome is not None
    assert strategy.outcome.content == "上一轮阶段成果"
    assert "超时" in (strategy.outcome.error or "")
    assert tool.calls == 2
    assert len(strategy.outcome.tool_calls) == 2
    assert strategy.outcome.tool_calls[0]["success"] is True
    assert strategy.outcome.tool_calls[1]["success"] is False, "未完成调用只能作为未知/失败回执，不能伪造成功"

    await tools.shutdown()


@pytest.mark.asyncio
async def test_react_context_window_exceeded_is_terminal():
    """预算闸拒绝（异常从 async_generate 上抛）→ CONTEXT_EXCEEDED 终结，保留进度语义。"""
    llm = _RaisingLLM(
        [{"finish_reason": "stop", "content": "不会到达"}],
        raise_on_call=1,
        exc=ContextWindowExceededError(model_key="main", input_tokens=1000, input_budget=100, max_tokens=1024),
    )
    strategy = ReActStrategy(llm=llm, tools=None)

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "请求上下文超限" in (strategy.outcome.error or "")
    assert "Agent 运行异常" not in (strategy.outcome.error or "")  # 未误归 UNKNOWN
    assert llm.calls == 1  # 终结性错误不重试
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_cancel_wins_when_context_error_arrives() -> None:
    """上下文异常与取消同时可见时，统一优先级必须选择 CANCELLED。"""
    cancel_event = asyncio.Event()

    class _CancelThenContextLLM(_RaisingLLM):
        async def async_generate(self, *args, **kwargs):
            cancel_event.set()
            async for event in super().async_generate(*args, **kwargs):
                yield event

    llm = _CancelThenContextLLM(
        [{"finish_reason": "stop", "content": "不会到达"}],
        exc=ContextWindowExceededError(model_key="main", input_tokens=1000, input_budget=100, max_tokens=1024),
    )
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, cancel_event=cancel_event
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.error == "Agent 已被取消"
    assert "请求上下文超限" not in strategy.outcome.error


@pytest.mark.asyncio
async def test_react_unknown_exception_continue_ignored():
    """UNKNOWN handler → CONTINUE 忽略：终结护栏仍 STOP 组装（部分进度保留）。"""

    async def on_unknown(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.UNKNOWN, on_unknown)

    llm = _RaisingLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "stop", "content": "不会到达"},
        ],
        raise_on_call=2,
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "Agent 运行异常" in (strategy.outcome.error or "")
    assert len(strategy.outcome.tool_calls) == 1  # 证据链保留


# ---------------------------------------------------------------
# 优雅取消（cancel_event）：用户停止 → CANCELLED 分发（不重试，保留部分进度）
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_cancel_event_stops():
    """cancel_event 置位 → 主循环顶部 CANCELLED 终止（error 记录取消，done 事件）。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "stop", "content": "不会到达"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)
    cancel_event = asyncio.Event()
    cancel_event.set()  # 预置位：第 1 轮顶部立即取消

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, cancel_event=cancel_event
        ),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "已被取消" in (strategy.outcome.error or "")
    assert strategy.outcome.iterations == 1
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_cancel_event_not_treated_as_llm_failed():
    """LLM error + cancel_event 置位 → CANCELLED（非 LLM_FAILED，不重试）。"""
    strategy = ReActStrategy(llm=_ErrorLLM(), tools=None)
    cancel_event = asyncio.Event()
    cancel_event.set()  # LLM 调用失败且取消信号置位 → 判取消而非失败

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, cancel_event=cancel_event
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "已被取消" in (strategy.outcome.error or "")
    assert strategy.outcome.iterations == 1


@pytest.mark.asyncio
async def test_react_cancel_event_untouched_normal():
    """cancel_event 未置位 → 正常完成（取消检查零开销）。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)
    cancel_event = asyncio.Event()  # 未置位

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, cancel_event=cancel_event
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.error is None


@pytest.mark.asyncio
async def test_react_post_call_cancel_wins_over_cost_and_keeps_usage():
    """流式响应归账时取消与成本同时命中，按 CANCELLED 收尾并保留本轮成果。"""
    cancel_event = asyncio.Event()
    usage = {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}

    class _CancelAfterResponseLLM(_ScriptedLLM):
        async def async_generate(self, *args, **kwargs):
            async for event in super().async_generate(*args, **kwargs):
                yield event
            cancel_event.set()

    llm = _CancelAfterResponseLLM([{"finish_reason": "stop", "content": "当前轮答案", "usage": usage}])
    strategy = ReActStrategy(llm=llm, tools=None, cost_limiter=_UsageCostLimiter())

    events = []
    async for event in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, cancel_event=cancel_event
        ),
    ):
        events.append(event)

    assert strategy.outcome is not None
    assert strategy.outcome.error == "Agent 已被取消"
    assert strategy.outcome.content == "当前轮答案"
    assert strategy.outcome.usage == usage
    assert len([e for e in events if '"type": "done"' in e]) == 1


@pytest.mark.asyncio
async def test_react_execute_timeout_first_iteration():
    """首轮 LLM 调用即超时 → 降级 outcome：success=False + error 记录超时 + iterations=1。"""
    strategy = ReActStrategy(llm=_SleepyLLM(sleep_before_call=1, delay=0.3), tools=None)

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=0.05
        ),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.content == ""
    assert "超时" in (strategy.outcome.error or "")
    assert strategy.outcome.iterations == 1
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_execute_timeout_keeps_partial_progress():
    """中途超时（第 2 轮 LLM sleep）→ 保留已完成轮次的工具调用记录。"""
    llm = _SleepyLLM(
        scripts=[
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ],
            },
            {"finish_reason": "stop", "content": "答案"},
        ],
        sleep_before_call=2,
        # 给首轮工具热加载留出稳定余量；第二轮仍确定超过总期限。
        delay=1.0,
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=0.5),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 2
    # 第 1 轮工具已执行完成，调用记录保留（部分进度证据）
    assert len(strategy.outcome.tool_calls) == 1
    assert strategy.outcome.tool_calls[0]["tool"] == "echo"
    assert "超时" in (strategy.outcome.error or "")


async def test_internal_deadline_keeps_current_round_partial_result_and_usage():
    """当前轮流式内容已产出后命中内部 deadline，TIMEOUT outcome 保留内容与用量。"""

    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}

    class _PartialDeadlineLLM:
        async def async_generate(self, *args, result=None, **kwargs):
            assert result is not None
            result.content = "当前轮部分答案"
            result.reasoning_content = "当前轮推理"
            yield build_message_event(result.content)
            raise LLMDeadlineExceededError("执行期限耗尽", usage=usage)

    strategy = ReActStrategy(llm=_PartialDeadlineLLM(), tools=None)
    events = []
    async for event in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=5.0),
    ):
        events.append(event)

    assert strategy.outcome is not None
    assert strategy.outcome.content == "当前轮部分答案"
    assert strategy.outcome.reasoning == "当前轮推理"
    assert strategy.outcome.usage == usage, "异常 usage 只应归账一次"
    assert strategy.outcome.total_tokens == 10
    assert "超时" in (strategy.outcome.error or "")
    assert any('"type": "done"' in event for event in events)


async def test_internal_deadline_empty_current_round_keeps_previous_result():
    """下一轮尚无可见产出便到期时，不用空 current_result 覆盖上一轮成果。"""

    class _SecondRoundDeadlineLLM:
        def __init__(self):
            self.calls = 0

        async def async_generate(self, *args, result=None, **kwargs):
            self.calls += 1
            assert result is not None
            if self.calls == 1:
                result.content = "上一轮阶段成果"
                result.finish_reason = "tool_calls"
                result.tool_calls = [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ]
                yield build_message_event(result.content)
                return
            raise LLMDeadlineExceededError("执行期限耗尽")
            yield  # pragma: no cover - 保持 async generator 形态

    strategy = ReActStrategy(llm=_SecondRoundDeadlineLLM(), tools=_make_registry(tools=[_EchoTool()]))
    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=5.0),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.content == "上一轮阶段成果"
    assert "超时" in (strategy.outcome.error or "")


@pytest.mark.parametrize("max_execution_time", [5.0, None])
async def test_inner_timeout_error_is_unknown_and_keeps_current_round(
    max_execution_time,
):
    """timeout scope 未到期时，LLM 内部 TimeoutError 应归 UNKNOWN 并保留当前轮成果。"""

    usage = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}

    class _FinishTimeoutLLM:
        async def async_generate(self, *args, result=None, **kwargs):
            assert result is not None
            result.content = "续接已经完成"
            result.reasoning_content = "当前轮推理"
            result.usage = usage
            yield build_message_event(result.content)
            raise TimeoutError("日志收尾超时")

    strategy = ReActStrategy(llm=_FinishTimeoutLLM(), tools=None)
    events = []
    async for event in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=1, temperature=0.2, max_tokens=1024, max_execution_time=max_execution_time
        ),
    ):
        events.append(event)

    assert strategy.outcome is not None
    assert strategy.outcome.error == "Agent 运行异常: TimeoutError"
    assert strategy.outcome.content == "续接已经完成"
    assert strategy.outcome.reasoning == "当前轮推理"
    assert strategy.outcome.usage == usage
    assert strategy.outcome.total_tokens == 15
    assert any('"type": "done"' in event for event in events)


async def test_unknown_exception_keeps_current_round():
    """普通收尾异常同样保留尚未正常归账的当前轮成果。"""

    class _FinishErrorLLM:
        async def async_generate(self, *args, result=None, **kwargs):
            assert result is not None
            result.content = "已生成内容"
            result.usage = {"total_tokens": 6}
            yield build_message_event(result.content)
            raise RuntimeError("日志收尾失败")

    strategy = ReActStrategy(llm=_FinishErrorLLM(), tools=None)
    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=1, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.error == "Agent 运行异常: RuntimeError"
    assert strategy.outcome.content == "已生成内容"
    assert strategy.outcome.usage == {"total_tokens": 6}


async def test_external_task_cancel_still_propagates_cancelled_error():
    """调用方硬取消 task 时不应被 UNKNOWN/TIMEOUT 降级吞掉。"""

    started = asyncio.Event()

    class _HangingLLM:
        async def async_generate(self, *args, **kwargs):
            started.set()
            await _hang_until_cancelled()
            yield  # pragma: no cover - 保持 async generator 形态

    strategy = ReActStrategy(llm=_HangingLLM(), tools=None)
    events = []

    async def _collect():
        async for event in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args(
                "react", max_iterations=1, temperature=0.2, max_tokens=1024, max_execution_time=5.0
            ),
        ):
            events.append(event)

    task = asyncio.create_task(_collect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert strategy.outcome is None
    assert not any('"type": "done"' in event for event in events)


async def test_execute_aclose_from_another_task_has_no_terminal_event():
    """异 task 关闭执行生成器时应干净退出，不伪造 UNKNOWN/TIMEOUT 终态。"""

    strategy = ReActStrategy(llm=_ScriptedLLM([]), tools=None)
    stream = strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=1, temperature=0.2, max_tokens=1024, max_execution_time=5.0),
    )
    first_event = await anext(stream)
    assert '"type": "agent_info"' in first_event

    await asyncio.create_task(stream.aclose())

    assert strategy.outcome is None


async def test_internal_deadline_cleanup_finishes_before_hard_timeout(monkeypatch):
    """内部 deadline 后的小段清理应在外层硬超时前完成。"""
    import app.domain.reasoning.react as react_module

    monkeypatch.setattr(react_module, "_EXECUTION_CLEANUP_GRACE_RATIO", 0.5)
    monkeypatch.setattr(react_module, "_MAX_EXECUTION_CLEANUP_GRACE", 0.1)
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _DeadlineCleanupLLM:
        async def async_generate(self, *args, result=None, deadline=None, **kwargs):
            assert deadline is not None
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            cleanup_started.set()
            await asyncio.sleep(0.01)
            cleanup_finished.set()
            raise LLMDeadlineExceededError("执行期限耗尽")
            yield  # pragma: no cover - 保持 async generator 形态

    strategy = ReActStrategy(llm=_DeadlineCleanupLLM(), tools=None)
    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=1, temperature=0.2, max_tokens=1024, max_execution_time=0.2),
    ):
        pass

    assert cleanup_started.is_set()
    assert cleanup_finished.is_set(), "硬超时不得与内部 deadline 同刻打断资源清理"
    assert strategy.outcome is not None
    assert "超时" in (strategy.outcome.error or "")


async def test_hard_timeout_keeps_current_round_partial_result(monkeypatch):
    """内部信号未生效而落入硬超时时，也应保留当前轮已产出的部分内容。"""
    import app.domain.reasoning.react as react_module

    monkeypatch.setattr(react_module, "_EXECUTION_CLEANUP_GRACE_RATIO", 0.1)
    monkeypatch.setattr(react_module, "_MAX_EXECUTION_CLEANUP_GRACE", 0.01)

    class _PartialThenHangLLM:
        async def async_generate(self, *args, result=None, **kwargs):
            assert result is not None
            result.content = "硬超时前的部分答案"
            yield build_message_event(result.content)
            await _hang_until_cancelled()

    strategy = ReActStrategy(llm=_PartialThenHangLLM(), tools=None)
    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=1, temperature=0.2, max_tokens=1024, max_execution_time=0.05
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.content == "硬超时前的部分答案"
    assert "超时" in (strategy.outcome.error or "")


async def test_deadline_unaware_llm_is_cancelled_at_timeout_trigger(monkeypatch):
    """忽略内部 deadline、但配合 task 取消的调用会在 timeout 触发点停止。"""
    import app.domain.reasoning.react as react_module

    monkeypatch.setattr(react_module, "_EXECUTION_CLEANUP_GRACE_RATIO", 0.25)
    monkeypatch.setattr(react_module, "_MAX_EXECUTION_CLEANUP_GRACE", 0.05)

    class _DeadlineUnawareLLM:
        async def async_generate(self, *args, **kwargs):
            await _hang_until_cancelled()
            yield  # pragma: no cover - 保持 async generator 形态

    strategy = ReActStrategy(llm=_DeadlineUnawareLLM(), tools=None)
    started = time.monotonic()
    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=1, temperature=0.2, max_tokens=1024, max_execution_time=0.05
        ),
    ):
        pass
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, "不识别内部 deadline 的调用应由 timeout task 取消终止"
    assert strategy.outcome is not None
    assert "超时" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_execute_loose_timeout_does_not_trigger():
    """宽松时间上限不影响正常完成。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=5.0),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.error is None


@pytest.mark.asyncio
async def test_react_execute_none_timeout_no_limit():
    """max_execution_time=None 显式不设限 → 正常完成。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=None
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.error is None


# ---------------------------------------------------------------
# 成本上限（增强项 #20）：累计成本超限 → COST_EXCEEDED 分发（默认 STOP 降级）
# ---------------------------------------------------------------


def _cost_limiter(ceiling: float | None = 0.05, model: str = "gpt-4"):
    from app.application.context.cost_limiter import CostLimiter
    from app.integration.llm.cost_tracker import CostTracker

    class _FakeLLM:
        """结构实现 LLMGateway.calculate_cost（镜像 LLMService 静态代理 CostTracker）。"""

        @staticmethod
        def calculate_cost(usage, model=""):
            return CostTracker.calculate(usage, model)

    return CostLimiter(ceiling=ceiling, llm=_FakeLLM(), model=model)


@pytest.mark.asyncio
async def test_react_baseline_cost_exceeded_stops_before_llm_call():
    """调用方累计基线已超成本时，首轮付费调用不得发出。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "不会调用"}])
    strategy = ReActStrategy(llm=llm, tools=None, cost_limiter=_cost_limiter(ceiling=0.01))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react",
            max_iterations=3,
            temperature=0.2,
            max_tokens=1024,
            baseline_usage={"prompt_tokens": 1000, "completion_tokens": 0},
        ),
    ):
        pass

    assert llm.calls == 0
    assert strategy.outcome is not None
    assert "成本超限" in (strategy.outcome.error or "")
    assert strategy.outcome.usage is None


@pytest.mark.asyncio
async def test_react_execute_cost_exceeded_stops():
    """首轮累计成本即超限 → STOP 降级：error 记录成本超限 + iterations=1 + done。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "stop",
                "content": "答案",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            }
        ]
    )
    # gpt-4：0.03 + 0.03 = 0.06 > ceiling 0.05
    strategy = ReActStrategy(llm=llm, tools=None, cost_limiter=_cost_limiter())

    events = []
    async for ev in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.iterations == 1
    # LLM 已产出内容 → 部分成功；error 记录成本超限
    assert strategy.outcome.success is True
    assert "成本超限" in (strategy.outcome.error or "")
    assert "0.0600" in (strategy.outcome.error or "")  # 累计成本进文案
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_execute_cost_exceeded_keeps_partial_progress():
    """第 2 轮累计成本超限 → 保留第 1 轮已完成工具调用记录。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},  # 0.009
            },
            {
                "finish_reason": "stop",
                "content": "答案",
                "usage": {"prompt_tokens": 500, "completion_tokens": 500},  # 累计 0.054
            },
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    # 第 1 轮 0.009 ≤ 0.05 不触发；第 2 轮累计 0.054 > 0.05 → 停机
    strategy = ReActStrategy(llm=llm, tools=tools, cost_limiter=_cost_limiter())

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.iterations == 2
    # 第 1 轮工具已执行完成，调用记录保留（部分进度证据）
    assert len(strategy.outcome.tool_calls) == 1
    assert strategy.outcome.tool_calls[0]["tool"] == "echo"
    assert "成本超限" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_execute_loose_cost_does_not_trigger():
    """宽松成本上限不影响正常完成。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "stop", "content": "完成", "usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, cost_limiter=_cost_limiter(ceiling=10.0))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.error is None


@pytest.mark.asyncio
async def test_react_execute_none_cost_no_limit():
    """cost_limiter=None（不注入）→ 正常完成，成本检查零开销不干扰。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)  # 不传 cost_limiter

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.error is None


@pytest.mark.asyncio
async def test_react_cost_exceeded_handler_raise():
    """COST_EXCEEDED handler → RAISE：抛 AgentRunError（kind=COST_EXCEEDED）。"""
    registry = ErrorHandlerRegistry()

    async def raise_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry.register(AgentErrorKind.COST_EXCEEDED, raise_handler)
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "stop",
                "content": "答案",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            }
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry, cost_limiter=_cost_limiter())

    with pytest.raises(AgentRunError) as exc:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc.value.kind == AgentErrorKind.COST_EXCEEDED
    assert "成本超限" in exc.value.message


@pytest.mark.asyncio
async def test_react_cost_exceeded_handler_continue_ignored():
    """COST_EXCEEDED handler → CONTINUE 被忽略：终结性护栏仍按 STOP 组装。"""
    registry = ErrorHandlerRegistry()

    async def continue_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry.register(AgentErrorKind.COST_EXCEEDED, continue_handler)
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "stop",
                "content": "答案",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            }
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry, cost_limiter=_cost_limiter())

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    # CONTINUE 不改变终结性护栏：outcome 已设置（STOP 组装），循环终止
    assert strategy.outcome is not None
    assert strategy.outcome.iterations == 1
    assert "成本超限" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_cost_baseline_usage_participates_in_check():
    """baseline_usage（跨阶段累计基线）计入成本判定：局部未超、基线+局部越界即在越界轮停。

    对照（无 baseline）3 轮累计 0.027 < ceiling 0.05 → 正常完成；带 baseline
    {600,200}=0.03 → 第 3 轮累计 0.057 > 0.05 → 停在第 3 轮（COST_EXCEEDED）。
    """
    scripts = [
        {
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": json.dumps({"text": "hi"})},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},  # 0.009
        },
        {
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "echo", "arguments": json.dumps({"text": "hi"})},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},  # 0.009
        },
        {
            "finish_reason": "stop",
            "content": "答案",
            "usage": {"prompt_tokens": 100, "completion_tokens": 100},  # 0.009
        },
    ]
    tools = _make_registry(tools=[_EchoTool()])

    # 对照：无 baseline → 3 轮累计 0.027 < 0.05，正常完成
    ctrl = ReActStrategy(llm=_ScriptedLLM(list(scripts)), tools=tools, cost_limiter=_cost_limiter())
    async for _ in ctrl.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass
    assert ctrl.outcome is not None and ctrl.outcome.success is True
    assert ctrl.outcome.error is None

    # 带 baseline 0.03 → 第 3 轮累计 0.057 越界 → 停在越界轮（iterations=3）
    base = ReActStrategy(llm=_ScriptedLLM(list(scripts)), tools=tools, cost_limiter=_cost_limiter())
    async for _ in base.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react",
            max_iterations=3,
            temperature=0.2,
            max_tokens=1024,
            baseline_usage={"prompt_tokens": 600, "completion_tokens": 200},
        ),
    ):
        pass
    assert base.outcome is not None
    assert base.outcome.iterations == 3
    assert "成本超限" in (base.outcome.error or "")


@pytest.mark.asyncio
async def test_react_cost_baseline_usage_not_in_reported_usage():
    """报告口径保持局部：baseline 只参与成本判定，不进 outcome.total_tokens（防双计）。"""
    usage_each = {
        "prompt_tokens": 100,
        "completion_tokens": 100,
        "total_tokens": 200,
    }
    scripts = [
        {
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "echo", "arguments": json.dumps({"text": "hi"})},
                }
            ],
            "usage": dict(usage_each),
        },
        {
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "echo", "arguments": json.dumps({"text": "hi"})},
                }
            ],
            "usage": dict(usage_each),
        },
        {
            "finish_reason": "stop",
            "content": "答案",
            "usage": dict(usage_each),
        },
    ]
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=_ScriptedLLM(scripts), tools=tools, cost_limiter=_cost_limiter())
    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react",
            max_iterations=3,
            temperature=0.2,
            max_tokens=1024,
            # baseline token 800 不计入报告；成本 0.03 + 局部 0.027 = 0.057 → 第 3 轮停
            baseline_usage={
                "prompt_tokens": 600,
                "completion_tokens": 200,
                "total_tokens": 800,
            },
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert "成本超限" in (strategy.outcome.error or "")  # baseline 参与了判定
    assert strategy.outcome.total_tokens == 600  # 3×200 局部，不含 baseline 800


@pytest.mark.asyncio
async def test_react_tool_failure_feedback_to_model():
    """工具失败 → tool 消息回喂错误文本，模型可感知失败原因并自愈。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fail", "arguments": "{}"},
                    }
                ],
            },
            {"finish_reason": "stop", "content": "已重试"},
        ]
    )
    tools = _make_registry(tools=[_FailingTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi", messages, **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024)
    ):
        pass

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "模拟执行失败" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_react_tool_failure_records_error_in_evidence():
    """工具失败 → outcome.tool_calls 记录 error / error_code（证据链）。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fail", "arguments": "{}"},
                    }
                ],
            },
            {"finish_reason": "stop", "content": "已重试"},
        ]
    )
    tools = _make_registry(tools=[_FailingTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    rec = strategy.outcome.tool_calls[0]
    assert rec["success"] is False
    assert rec["error"] == "模拟执行失败"
    assert rec["error_code"] is None  # 业务失败无系统错误码


@pytest.mark.asyncio
async def test_react_unknown_tool_feedback():
    """无效工具名 → 回喂「未注册」错误 + 证据链记录 NOT_REGISTERED。"""
    tools = _make_registry(tools=[_EchoTool()])  # 未注册 no_such_tool
    strategy = ReActStrategy(llm=_NoopLLM(), tools=tools)
    tool_calls = [
        {
            "id": "call_x",
            "type": "function",
            "function": {"name": "no_such_tool", "arguments": "{}"},
        }
    ]
    messages = []

    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1, run=reasoning_run_scope()):
        pass

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "未注册" in tool_msgs[0]["content"]
    # 证据链记录：系统错误码 NOT_REGISTERED
    assert strategy._tool_call_records[0]["error_code"] == "NOT_REGISTERED"


@pytest.mark.asyncio
async def test_react_parse_failure_no_execute_and_feedback():
    """工具参数 JSON 解析失败 → 不执行工具 + 回喂解析失败 + 证据链 JSON_PARSE。"""
    probe = _DelayTool("probe", delay=0.001)
    tools = _make_registry(tools=[probe])
    strategy = ReActStrategy(llm=_NoopLLM(), tools=tools)
    tool_calls = [
        {
            "id": "call_p",
            "type": "function",
            "function": {"name": "probe", "arguments": "{invalid json"},
        }
    ]
    messages = []

    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1, run=reasoning_run_scope()):
        pass

    # 工具未被真正执行（解析失败短路，避免空参执行的错误掩盖 / 副作用）
    assert probe.exec_started == []
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "解析失败" in tool_msgs[0]["content"]
    # 证据链：系统错误码 JSON_PARSE + 失败原因
    assert strategy._tool_call_records[0]["error_code"] == "JSON_PARSE"
    assert "解析失败" in strategy._tool_call_records[0]["error"]


@pytest.mark.asyncio
async def test_react_long_tool_result_truncated_marker():
    """长工具结果回喂截断时带 [结果已截断] 标记，模型可知结果不完整。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "x" * 3000}),
                        },
                    }
                ],
            },
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi", messages, **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024)
    ):
        pass

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert "[结果已截断]" in content
    assert len(content) <= 2000  # 含标记不超限（预留标记长度）


@pytest.mark.asyncio
async def test_react_short_tool_result_no_marker():
    """短工具结果（不截断）回喂无截断标记。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ],
            },
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi", messages, **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024)
    ):
        pass

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "[结果已截断]" not in tool_msgs[0]["content"]
    assert tool_msgs[0]["content"] == "echo:hi"


@pytest.mark.asyncio
async def test_react_reasoning_feedback_when_has_reasoning():
    """has_reasoning=True 且 reasoning_content 空时，assistant 仍保留该字段。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "has_reasoning": True,
                "reasoning_content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ],
            },
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi", messages, **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024)
    ):
        pass

    # 第 1 轮 assistant 消息（含 tool_calls）应带 reasoning_content 键（空串）
    first_assistant = next(m for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
    assert "reasoning_content" in first_assistant
    assert first_assistant["reasoning_content"] == ""


@pytest.mark.asyncio
async def test_react_reasoning_no_feedback_without_signal():
    """无 has_reasoning（chat 模型）→ assistant 消息不带 reasoning_content 键。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi", messages, **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024)
    ):
        pass

    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert "reasoning_content" not in assistant_msgs[0]


@pytest.mark.asyncio
async def test_react_context_budget_trims_rounds():
    """注入 ContextBudgetPort（ContextManager）后：每轮 trim，assistant 轮数保持 <= max_context_rounds。"""
    from app.application.context.context_manager import ContextManager
    from app.integration.llm.token_counter import TiktokenTokenCounter

    budget = ContextManager(
        session_manager=object(),  # trim_messages 不使用 session_manager
        llm=TiktokenTokenCounter("gpt-4"),
    )
    echo_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "echo", "arguments": json.dumps({"text": "hi"})},
    }
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [echo_call]},
            {"finish_reason": "tool_calls", "tool_calls": [echo_call]},
            {"finish_reason": "tool_calls", "tool_calls": [echo_call]},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools, context_budget=budget)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi",
        messages,
        **reasoning_execution_args("react", max_iterations=4, temperature=0.2, max_tokens=1024, max_context_rounds=2),
    ):
        pass

    # 3 轮 tool_calls：第 1 轮被 trim，保留最近 2 轮；最后 stop 轮不触发 trim
    # → assistant = 最近 2 轮 + 最终回答 = 3；tool = 最近 2 轮的工具结果 = 2
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    assert assistant_count == 3
    assert tool_count == 2
    assert messages[0]["role"] == "user"  # 前缀保留


@pytest.mark.asyncio
async def test_react_context_budget_trims_on_no_tool_retry():
    """上下文预算：非工具路径（空输出重试）每次 LLM 调用前也裁剪——预算不能只在工具路径生效。"""
    from app.application.context.context_manager import ContextManager
    from app.integration.llm.token_counter import TiktokenTokenCounter

    budget = ContextManager(
        session_manager=object(),  # trim_messages 不使用 session_manager
        llm=TiktokenTokenCounter("gpt-4"),
    )
    # 无工具：每轮空输出重试（_handle_empty_output CONTINUE），不走 _handle_tool_calls
    strategy = ReActStrategy(llm=_EmptyLLM(), tools=None, context_budget=budget)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi",
        messages,
        **reasoning_execution_args(
            "react", max_iterations=6, temperature=0.2, max_tokens=1024, max_context_rounds=2, max_empty_retries=5
        ),  # 大上限保持 6 轮空输出重试，验证预算裁剪
    ):
        pass

    # 每轮顶部 trim：assistant 保持 <= max_context_rounds（末轮 append 未再 trim，+1）
    # 修复前预算仅工具路径生效 → 6 轮空输出重试无裁剪 → assistant=6
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
    assert assistant_count <= 3
    assert messages[0]["role"] == "user"  # 前缀保留


# final_answer 结构化输出测试用的 JSON Schema
_FA_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "conclusion": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["conclusion", "confidence"],
}


@pytest.mark.parametrize(
    "schema",
    [
        {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"},
        {"properties": {"x": {"$schema": "urn:unknown"}}},
        {"type": "object", "properties": 5},
    ],
)
async def test_final_answer_schema_preflight_stops_before_llm(schema: dict) -> None:
    """错误定义在注入 terminal tool 前拒绝，不能回喂给模型修改。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "unused"}])
    strategy = ReActStrategy(llm=llm, tools=None)
    with pytest.raises(SchemaError):
        async for _ in strategy.execute(
            "hi",
            [],
            **reasoning_execution_args(
                "react", output_schema=schema, max_iterations=3, temperature=0.2, max_tokens=1024
            ),
        ):
            pass
    assert llm.calls == 0
    assert strategy.outcome is None


async def test_final_answer_202012_dependency_feedback_then_success() -> None:
    """2020-12 字段依赖失败回喂，修正后终止；保持协议调用计数。"""
    schema = {
        "type": "object",
        "properties": {"equipment": {}, "time": {}},
        "dependentRequired": {"equipment": ["time"]},
    }
    good = {"equipment": "EQ-01", "time": "today"}
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": f"fa-{index}",
                        "type": "function",
                        "function": {"name": "final_answer", "arguments": json.dumps(args)},
                    }
                ],
            }
            for index, args in enumerate([{"equipment": "EQ-01"}, good])
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)
    events = [
        event
        async for event in strategy.execute(
            "hi",
            [],
            **reasoning_execution_args(
                "react", output_schema=schema, max_iterations=3, temperature=0.2, max_tokens=1024
            ),
        )
    ]
    assert llm.calls == 2
    assert strategy.outcome.success
    assert strategy.outcome.structured == good
    assert sum('"type": "done"' in event for event in events) == 1


@pytest.mark.asyncio
async def test_react_final_answer_success():
    """模型调用 final_answer（合法参数）→ outcome.structured 提取 + 循环终止。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "fa1",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": json.dumps({"conclusion": "根因A", "confidence": 0.9}),
                        },
                    }
                ],
            },
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    messages = [{"role": "user", "content": "hi"}]
    events = []
    async for ev in strategy.execute(
        "hi",
        messages,
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, output_schema=_FA_REPORT_SCHEMA
        ),
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.structured == {"conclusion": "根因A", "confidence": 0.9}
    assert strategy.outcome.iterations == 1
    # 循环终止：final_answer 未走工具执行（注入工具非注册），无工具调用记录
    assert strategy.outcome.tool_calls == []
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_react_final_answer_validation_failure_feedback():
    """final_answer 参数缺必填 → 回喂错误 + 证据链 VALIDATION + 循环继续（下轮 stop）。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "fa1",
                        "type": "function",
                        "function": {"name": "final_answer", "arguments": "{}"},
                    }
                ],
            },
            {"finish_reason": "stop", "content": "自由文本"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi",
        messages,
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, output_schema=_FA_REPORT_SCHEMA
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.structured is None  # 模型未产出合法结构化
    assert strategy.outcome.content == "自由文本"
    # 证据链记录 VALIDATION + 失败原因
    assert strategy._tool_call_records[0]["error_code"] == "VALIDATION"
    assert "不符合 schema" in strategy._tool_call_records[0]["error"]
    # tool 消息回喂错误文本（模型可自纠）
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "不符合 schema" in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_react_no_output_schema_structured_none():
    """未配置 output_schema → 不注入 final_answer，自由文本结束 structured=None。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.structured is None
    assert strategy.outcome.content == "完成"


@pytest.mark.asyncio
async def test_react_llm_failed_handler_continue_retries():
    """自定义 LLM_FAILED handler → CONTINUE：失败后重试，下轮成功。"""

    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    llm = _ScriptedLLM(
        [
            {"error": "401 认证失败"},
            {"finish_reason": "stop", "content": "成功"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "成功"
    assert strategy.outcome.iterations == 2


@pytest.mark.asyncio
async def test_react_llm_failed_handler_raise():
    """自定义 LLM_FAILED handler → RAISE：抛 AgentError（携带 kind）。"""

    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    strategy = ReActStrategy(llm=_ErrorLLM(), tools=None, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.LLM_FAILED


@pytest.mark.asyncio
async def test_react_empty_output_handler_stop():
    """自定义 EMPTY_OUTPUT handler → STOP：空输出直接终止（默认是重试）。"""

    async def on_empty(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.EMPTY_OUTPUT, on_empty)

    strategy = ReActStrategy(llm=_EmptyLLM(), tools=None, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "终止" in (strategy.outcome.error or "")
    assert strategy.outcome.iterations == 1


@pytest.mark.asyncio
async def test_react_tool_failed_handler_stop():
    """自定义 TOOL_FAILED handler → STOP：工具失败即终止（部分进度保留）。"""

    async def on_tool_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.TOOL_FAILED, on_tool_failed)

    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fail", "arguments": "{}"},
                    }
                ],
            },
            {"finish_reason": "stop", "content": "已重试"},
        ]
    )
    tools = _make_registry(tools=[_FailingTool()])
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "终止" in (strategy.outcome.error or "")
    # 部分进度保留：本轮工具失败记录仍在证据链
    assert len(strategy.outcome.tool_calls) == 1
    assert strategy.outcome.tool_calls[0]["success"] is False


@pytest.mark.asyncio
async def test_react_structured_invalid_handler_stop():
    """自定义 STRUCTURED_INVALID handler → STOP：final_answer 校验失败即终止。"""

    async def on_struct(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.STRUCTURED_INVALID, on_struct)

    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "fa1",
                        "type": "function",
                        "function": {"name": "final_answer", "arguments": "{}"},
                    }
                ],
            },
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, output_schema=_FA_REPORT_SCHEMA
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "终止" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_tool_failures_grouped_by_kind():
    """同 kind 多个失败聚合为一条 message 分发（handler 可见全部原因），CONTINUE 继续。"""
    seen: list[str] = []

    async def on_tool_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        seen.append(ctx.message)
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.TOOL_FAILED, on_tool_failed)

    tools = _make_registry(
        tools=[
            _FailingToolNamed("fail_a", error="原因A"),
            _FailingToolNamed("fail_b", error="原因B"),
        ]
    )
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "fail_a", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "fail_b", "arguments": "{}"}},
                ],
            },
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    # 同 kind 聚合为一条 message，含两个工具名与原因
    assert len(seen) == 1
    assert "fail_a" in seen[0] and "原因A" in seen[0]
    assert "fail_b" in seen[0] and "原因B" in seen[0]
    # CONTINUE：回喂后继续，下轮 stop 正常完成
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"


@pytest.mark.asyncio
async def test_react_tool_failures_stop_arbitration():
    """跨 kind：TOOL_FAILED→CONTINUE + PARSE_FAILED→STOP → 终止（STOP 优先于 CONTINUE）。"""

    async def on_tool_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    async def on_parse_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.TOOL_FAILED, on_tool_failed)
    registry.register(AgentErrorKind.PARSE_FAILED, on_parse_failed)

    tools = _make_registry(
        tools=[
            _FailingToolNamed("fail_a"),
            _DelayTool("probe", delay=0),
        ]
    )
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "fail_a", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "probe", "arguments": "{bad"}},
                ],
            },
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "终止" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_tool_failures_raise_arbitration():
    """跨 kind：PARSE_FAILED→RAISE + TOOL_FAILED→CONTINUE → 上报（RAISE 优先）。"""

    async def on_tool_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    async def on_parse_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.TOOL_FAILED, on_tool_failed)
    registry.register(AgentErrorKind.PARSE_FAILED, on_parse_failed)

    tools = _make_registry(
        tools=[
            _FailingToolNamed("fail_a"),
            _DelayTool("probe", delay=0),
        ]
    )
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "fail_a", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "probe", "arguments": "{bad"}},
                ],
            },
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.PARSE_FAILED


@pytest.mark.asyncio
async def test_react_execute_tool_calls_parallel_preserves_order():
    """execute_tool_calls：tool_messages 顺序保持 = tool_calls 输入顺序。"""
    reg = _make_registry(
        max_concurrent=10,
        tools=[
            _DelayTool("tool_a", delay=0.03),
            _DelayTool("tool_b", delay=0.01),
            _DelayTool("tool_c", delay=0.02),
        ],
    )

    strategy = ReActStrategy(llm=_NoopLLM(), tools=reg)
    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "tool_a", "arguments": json.dumps({"query": "x1"})}},
        {"id": "call_2", "type": "function", "function": {"name": "tool_b", "arguments": json.dumps({"query": "x2"})}},
        {"id": "call_3", "type": "function", "function": {"name": "tool_c", "arguments": json.dumps({"query": "x3"})}},
    ]
    messages = []

    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1, run=reasoning_run_scope()):
        pass

    # tool_messages 顺序 = 输入顺序（gather 保序）
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["call_1", "call_2", "call_3"]
    assert [m["content"] for m in messages if m["role"] == "tool"] == ["tool_a:x1", "tool_b:x2", "tool_c:x3"]


@pytest.mark.asyncio
async def test_react_execute_tool_calls_actually_concurrent():
    """execute_tool_calls：并行执行总耗时 < 串行和。"""
    reg = _make_registry(
        max_concurrent=10,
        tools=[_DelayTool("tool_a", delay=0.05), _DelayTool("tool_b", delay=0.05)],
    )

    strategy = ReActStrategy(llm=_NoopLLM(), tools=reg)
    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
        {"id": "call_2", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}},
    ]
    messages = []

    start = time.monotonic()
    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1, run=reasoning_run_scope()):
        pass
    elapsed = time.monotonic() - start

    # 并行执行两个 0.05s 工具，总耗时应 < 串行 0.1s（留余量，断言 < 0.09）
    assert elapsed < 0.09, f"应并行执行（<0.09s），实际 {elapsed:.3f}s"


class _NoopLLM:
    """最小 LLM 替身（execute_tool_calls 不真正调用 LLM）。"""

    async def async_generate(self, *args, **kwargs):
        yield ""


# ======================================================================
# 协议异常：finish_reason=tool_calls 但未返回工具调用 → PARSE_FAILED 分发
# ======================================================================


@pytest.mark.asyncio
async def test_react_protocol_error_retry_then_success():
    """finish_reason=tool_calls 但 tool_calls 空 → 默认 CONTINUE 重试，下一轮正常结束。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls"},  # 声明调工具但未给出 tool_calls
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])  # 注册工具：修复前会空转执行空列表
    strategy = ReActStrategy(llm=llm, tools=tools)

    events = []
    async for event in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        events.append(event)

    # 协议异常轮重试（LLM 被调 2 次）→ 第 2 轮 stop 成功结束
    assert llm.calls == 2
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    # 不进 execute_tool_calls 空转（无工具记录，不 yield「检测到 0 个工具调用」荒谬信息）
    assert strategy.outcome.tool_calls == []
    assert not any("检测到 0 个工具调用" in e for e in events)


@pytest.mark.asyncio
async def test_react_protocol_error_not_counted_as_empty_output():
    """协议异常不入空输出计数，默认第 3 轮由独立协议修正上限终止。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls"},
            {"finish_reason": "tool_calls"},
            {"finish_reason": "tool_calls"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    # 两种预算默认均为 2，但错误归因必须是协议异常而非空输出。
    assert strategy.outcome is not None
    assert "连续工具调用协议异常" in (strategy.outcome.error or "")
    assert "连续空输出" not in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_protocol_error_handler_stop():
    """PARSE_FAILED handler → STOP：协议异常直接终止（error 记录）。"""

    async def on_parse(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.PARSE_FAILED, on_parse)

    strategy = ReActStrategy(
        llm=_ScriptedLLM([{"finish_reason": "tool_calls"}]),
        tools=None,
        error_handlers=registry,
    )

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "协议异常" in (strategy.outcome.error or "")
    assert strategy.outcome.iterations == 1


@pytest.mark.asyncio
async def test_react_protocol_error_handler_raise():
    """PARSE_FAILED handler → RAISE：抛 AgentRunError（携带 kind）。"""

    async def on_parse(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.PARSE_FAILED, on_parse)

    strategy = ReActStrategy(
        llm=_ScriptedLLM([{"finish_reason": "tool_calls"}]),
        tools=None,
        error_handlers=registry,
    )

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.PARSE_FAILED


@pytest.mark.asyncio
async def test_react_protocol_error_no_tools():
    """无工具注册（has_tools=False）时同样识别为协议异常，不落入空输出分支。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls"},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)  # 修复前：无工具 → 空输出分支重试

    events = []
    async for event in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        events.append(event)

    assert llm.calls == 2
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    # 走协议异常重试而非空输出重试（事件可区分）
    assert any("协议异常" in e for e in events)
    assert not any("LLM 未生成有效输出" in e for e in events)


@pytest.mark.asyncio
async def test_react_protocol_error_no_tools_with_tool_calls():
    """无工具注册 + finish_reason=tool_calls + 非空 tool_calls → 协议异常短路（不进空输出/空转执行）。"""
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ],
            },
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)  # 未注册任何工具

    messages = [{"role": "user", "content": "hi"}]
    events = []
    async for event in strategy.execute(
        "hi",
        messages,
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        events.append(event)

    # 协议异常重试 → 下一轮 stop 正常结束（修复前：非空 tool_calls 使空输出计数清零，
    # 误入空输出分支且 max_empty_retries 失效，仅靠 max_iterations 兜底）
    assert llm.calls == 2
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    # 走协议异常重试而非空输出重试（事件可区分）；无工具执行记录
    assert any("协议异常" in e for e in events)
    assert not any("LLM 未生成有效输出" in e for e in events)
    assert strategy.outcome.tool_calls == []
    # 无工具可用时该 tool_calls 响应无从配对，不能写入下一轮请求历史。
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "完成"


@pytest.mark.asyncio
async def test_react_tool_protocol_retry_limit_hard_stops():
    """持续协议异常只允许 N 次修正，第 N+1 次硬终止。"""
    llm = _ScriptedLLM([{"finish_reason": "tool_calls"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=10, temperature=0.2, max_tokens=1024, max_tool_protocol_retries=2
        ),
    ):
        pass

    assert llm.calls == 3
    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "连续工具调用协议异常（3 轮）" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_tool_protocol_retry_limit_zero_stops_first_failure():
    """协议修正上限为 0 时，首次异常即终止。"""
    llm = _ScriptedLLM([{"finish_reason": "tool_calls"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=10, temperature=0.2, max_tokens=1024, max_tool_protocol_retries=0
        ),
    ):
        pass

    assert llm.calls == 1
    assert strategy.outcome is not None
    assert "连续工具调用协议异常（1 轮）" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_tool_protocol_retry_count_resets_after_valid_tool_round():
    """合法工具协议轮证明协议恢复，连续异常计数随即清零。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls"},
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "tool_calls"},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=6, temperature=0.2, max_tokens=1024, max_tool_protocol_retries=1
        ),
    ):
        pass

    assert llm.calls == 4
    assert strategy.outcome is not None
    assert strategy.outcome.success is True


@pytest.mark.asyncio
async def test_react_llm_failure_does_not_reset_tool_protocol_retry_count():
    """传输失败没有证明工具协议恢复，不能清零协议修正计数。"""

    async def continue_llm_failure(
        ctx: AgentErrorContext,
    ) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, continue_llm_failure)
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls"},
            {"error": "临时传输失败"},
            {"finish_reason": "tool_calls"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=6, temperature=0.2, max_tokens=1024, max_tool_protocol_retries=1
        ),
    ):
        pass

    assert llm.calls == 3
    assert strategy.outcome is not None
    assert "连续工具调用协议异常（2 轮）" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_invalid_final_answer_obeys_tool_protocol_retry_limit():
    """final_answer 持续校验失败与工具协议异常共享修正上限。"""
    bad_final = {
        "id": "fa_bad",
        "type": "function",
        "function": {"name": "final_answer", "arguments": "{}"},
    }
    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [bad_final]}])
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react",
            max_iterations=10,
            temperature=0.2,
            max_tokens=1024,
            output_schema=_FA_REPORT_SCHEMA,
            max_tool_protocol_retries=1,
        ),
    ):
        pass

    assert llm.calls == 2
    assert strategy.outcome is not None
    assert "连续工具调用协议异常（2 轮）" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_invalid_tool_arguments_obey_tool_protocol_retry_limit():
    """变化的非法 JSON 参数不能靠改变指纹绕过协议修正上限。"""
    scripts = []
    for index in range(3):
        scripts.append(
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": f"bad_{index}",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": f"{{bad-{index}",
                        },
                    }
                ],
            }
        )
    llm = _ScriptedLLM(scripts)
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=10, temperature=0.2, max_tokens=1024, max_tool_protocol_retries=1
        ),
    ):
        pass

    assert llm.calls == 2
    assert strategy.outcome is not None
    assert "连续工具调用协议异常（2 轮）" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_unknown_final_answer_name_does_not_bypass_stall_limit():
    """未启用 output_schema 时同名工具只是普通名称，同参数重复调用仍受停滞上限约束。"""
    call = {
        "id": "unknown_fa",
        "type": "function",
        "function": {"name": "final_answer", "arguments": "{}"},
    }
    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [call]}] * 3)
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=5, temperature=0.2, max_tokens=1024, max_same_action_turns=1
        ),
    ):
        pass

    assert llm.calls == 2
    assert strategy.outcome is not None
    assert "相同工具调用" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_unknown_final_answer_arguments_enter_stall_fingerprint():
    """同名 final_answer 的参数参与指纹：参数各异的连续轮次不得被误判为同一动作。

    旧实现按名排除该调用，整轮仅它时指纹恒为 "[]"，参数不同的轮次会被提前判定停滞。
    """
    scripts = [
        {
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": f"unknown_fa_{index}",
                    "type": "function",
                    "function": {"name": "final_answer", "arguments": f'{{"i": {index}}}'},
                }
            ],
        }
        for index in range(3)
    ]
    llm = _ScriptedLLM(scripts)
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, max_same_action_turns=1
        ),
    ):
        pass

    assert llm.calls == 3, "参数不同的同名调用不应触发停滞硬终止"
    assert strategy.outcome is not None
    assert "相同工具调用" not in (strategy.outcome.error or "")


# ======================================================================
# LLM 失败重试上限（max_llm_fail_retries）：对齐空输出护栏防无限重试烧钱
# ======================================================================


@pytest.mark.asyncio
async def test_react_llm_fail_retries_hard_stop():
    """LLM 持续失败 + handler CONTINUE → 连续失败超 max_llm_fail_retries 硬终止。"""

    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    llm = _ScriptedLLM(
        [
            {"error": "401 认证失败"},
            {"error": "401 认证失败"},
            {"error": "401 认证失败"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=5, temperature=0.2, max_tokens=1024),
    ):
        pass

    # 默认 max_llm_fail_retries=2：第 3 次失败（计数 3 > 2）硬终止，即使 handler CONTINUE
    assert llm.calls == 3
    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "连续 LLM 调用失败（3 轮）" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_llm_fail_retries_resets_on_success():
    """成功轮清零失败计数：失败→工具成功→再失败不累计硬终止（对齐空输出「有产出清零」）。"""

    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    llm = _ScriptedLLM(
        [
            {"error": "401 认证失败"},
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"error": "401 认证失败"},
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=6, temperature=0.2, max_tokens=1024, max_llm_fail_retries=1),
    ):
        pass

    # max_llm_fail_retries=1 + 失败/工具成功交替：工具轮（error None）清零失败计数，
    # 两次 error 各自计数 1 ≤ 1 不累计硬终止（若不清零，第 3 轮 error 计数 2 > 1 硬终止）
    assert llm.calls == 5
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"


@pytest.mark.asyncio
async def test_react_llm_fail_retries_handler_raise():
    """连续失败达上限硬终止 → handler RAISE：抛 AgentRunError(LLM_FAILED)。"""

    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE if "连续 LLM 调用失败" in ctx.message else AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    llm = _ScriptedLLM(
        [
            {"error": "401 认证失败"},
            {"error": "401 认证失败"},
            {"error": "401 认证失败"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=5, temperature=0.2, max_tokens=1024),
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.LLM_FAILED
    assert "连续 LLM 调用失败" in exc_info.value.message


@pytest.mark.asyncio
async def test_react_llm_fail_retries_zero():
    """max_llm_fail_retries=0 → 首次失败即终止（handler CONTINUE 忽略）。"""

    async def on_llm_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.CONTINUE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, on_llm_failed)

    strategy = ReActStrategy(llm=_ErrorLLM(), tools=None, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=5, temperature=0.2, max_tokens=1024, max_llm_fail_retries=0),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "连续 LLM 调用失败（1 轮）" in (strategy.outcome.error or "")
    assert strategy.outcome.iterations == 1


@pytest.mark.asyncio
async def test_react_llm_fail_retries_default_stop_unchanged():
    """默认 STOP（无 CONTINUE handler）→ 首次失败短路（计数不影响既有行为）。"""
    strategy = ReActStrategy(llm=_ErrorLLM(), tools=None)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=5, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "401 认证失败" in (strategy.outcome.error or "")
    assert strategy.outcome.iterations == 1


# ======================================================================
# 空输出重试轮不追加空 assistant 消息（避免累积污染上下文）
# ======================================================================


@pytest.mark.asyncio
async def test_react_empty_output_retry_no_blank_assistant():
    """空输出重试轮不追加空 assistant 消息（修复前连续空输出累积空消息污染上下文）。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "", "content": ""},
            {"finish_reason": "", "content": ""},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    async for _ in strategy.execute(
        "hi", messages, **reasoning_execution_args("react", max_iterations=5, temperature=0.2, max_tokens=1024)
    ):
        pass

    # 连续 2 轮空输出重试：消息历史只含 user + 最终 assistant，不含空 assistant 记录
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "完成"


# ======================================================================
# 工具级 timeout/max_retries 接线：execute() 透传到 ToolGateway.execute
# ======================================================================


class _RecordingGateway:
    """记录 execute 参数（timeout/max_retries）的 ToolGateway mock。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get_openai_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "回声工具",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
        ]

    async def execute(
        self,
        name: str,
        parameters: dict,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
        *,
        call,
        facts,
    ) -> ToolResult:
        self.calls.append(
            {
                "name": name,
                "params": parameters,
                "timeout": timeout,
                "max_retries": max_retries,
                "call": call,
                "facts": facts,
            }
        )
        return ToolResult(success=True, content=f"echo:{parameters.get('text', '')}")


@pytest.mark.asyncio
async def test_react_tool_timeout_retries_passed_to_gateway():
    """execute() 的 tool_timeout/tool_max_retries 透传到 ToolGateway.execute。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    gateway = _RecordingGateway()
    strategy = ReActStrategy(llm=llm, tools=gateway)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, tool_timeout=60, tool_max_retries=5
        ),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["timeout"] == 60
    assert gateway.calls[0]["max_retries"] == 5


@pytest.mark.asyncio
async def test_react_tool_timeout_retries_default_none():
    """不传 tool_timeout/tool_max_retries → gateway 收到 None（走执行器全局/工具默认）。"""
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [_echo_call()]},
            {"finish_reason": "stop", "content": "完成"},
        ]
    )
    gateway = _RecordingGateway()
    strategy = ReActStrategy(llm=llm, tools=gateway)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert gateway.calls[0]["timeout"] is None
    assert gateway.calls[0]["max_retries"] is None


@pytest.mark.asyncio
async def test_execute_tool_calls_tool_execution_default_and_override():
    """原语直接复用时：默认不覆盖工具执行选项，显式传入则整组透传给网关。"""
    gateway = _RecordingGateway()
    strategy = ReActStrategy(llm=_NoopLLM(), tools=gateway)
    tool_calls = [_echo_call()]
    messages = []

    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1, run=reasoning_run_scope()):
        pass

    assert gateway.calls[0]["timeout"] is None
    assert gateway.calls[0]["max_retries"] is None

    async for _ in strategy.execute_tool_calls(
        tool_calls,
        messages,
        iteration=1,
        run=reasoning_run_scope(),
        tool_execution=ToolExecutionOptions(timeout=60, max_attempts=5),
    ):
        pass

    assert gateway.calls[1]["timeout"] == 60
    assert gateway.calls[1]["max_retries"] == 5


# ======================================================================
# 问题 4：错误处理 handler 自身异常 → 防御降级（不破坏主循环）
# ======================================================================


@pytest.mark.asyncio
async def test_react_handler_exception_llm_failed_default_stop():
    """LLM_FAILED handler 抛异常 → 降级为默认 STOP 短路（不崩，error 为 LLM 失败原因）。"""

    async def broken_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        raise RuntimeError("handler bug")

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.LLM_FAILED, broken_handler)

    strategy = ReActStrategy(llm=_ErrorLLM(), tools=None, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    # 不抛 handler 异常；按默认 STOP 短路，error 记录 LLM 失败原因（非 handler bug）
    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "401 认证失败" in (strategy.outcome.error or "")
    assert "handler bug" not in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_handler_exception_tool_failed_default_continue():
    """TOOL_FAILED handler 抛异常 → 降级为默认 CONTINUE（回喂继续，下一轮正常结束）。"""

    async def broken_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        raise RuntimeError("handler bug")

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.TOOL_FAILED, broken_handler)

    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "fail", "arguments": "{}"},
                    }
                ],
            },
            {"finish_reason": "stop", "content": "已重试"},
        ]
    )
    tools = _make_registry(tools=[_FailingTool()])
    strategy = ReActStrategy(llm=llm, tools=tools, error_handlers=registry)

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    # handler 异常不破坏主循环：工具失败回喂继续，下一轮 stop 正常结束
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "已重试"
    assert strategy.outcome.iterations == 2


@pytest.mark.asyncio
async def test_react_unknown_handler_exception_not_breaking():
    """UNKNOWN handler 抛异常 → 主循环 UNKNOWN 兜底不崩（默认 STOP 保留部分进度）。"""

    async def broken_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        raise RuntimeError("handler bug")

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.UNKNOWN, broken_handler)

    # _RaisingLLM 第一轮抛 RuntimeError → 主循环 UNKNOWN 分发
    strategy = ReActStrategy(
        llm=_RaisingLLM([{"finish_reason": "stop", "content": "x"}], raise_on_call=1),
        tools=None,
        error_handlers=registry,
    )

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
    ):
        pass

    # 修复前：UNKNOWN handler 异常从 except 块逃逸 → execute() 调用方收到异常；
    # 修复后：降级默认 STOP，outcome 正常组装（保留部分进度）
    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "Agent 运行异常" in (strategy.outcome.error or "")


# ======================================================================
# 问题 5：UNKNOWN error 脱敏——只保留异常类型名，完整异常进日志
# ======================================================================


@pytest.mark.asyncio
async def test_react_unknown_error_redacts_exception_message(caplog):
    """UNKNOWN error 脱敏：异常 message（含敏感值/内部路径）不进入产品侧文本，完整异常进日志。"""
    llm = _RaisingLLM(
        [{"finish_reason": "stop", "content": "x"}],
        raise_on_call=1,
        exc=RuntimeError("连接失败: 内部端点 http://10.0.0.1/api key=sk-secret"),
    )
    strategy = ReActStrategy(llm=llm, tools=None)

    with caplog.at_level("ERROR", logger="app.domain.reasoning.react"):
        async for _ in strategy.execute(
            "hi",
            [{"role": "user", "content": "hi"}],
            **reasoning_execution_args("react", max_iterations=3, temperature=0.2, max_tokens=1024),
        ):
            pass

    # error 只保留异常类型名（分类），message（内部端点/敏感 key）不泄漏到产品侧
    assert strategy.outcome is not None
    assert strategy.outcome.error == "Agent 运行异常: RuntimeError"
    assert "sk-secret" not in (strategy.outcome.error or "")
    assert "10.0.0.1" not in (strategy.outcome.error or "")
    # 完整异常（含 message）进日志，运维可诊断
    assert any("sk-secret" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_react_empty_output_round_does_not_reset_protocol_budget():
    """空输出不证明工具协议恢复：间隔的空输出轮不得清零协议修正连续计数。"""
    bad_calls = [
        {
            "id": f"bad_{index}",
            "type": "function",
            "function": {"name": "echo", "arguments": f"{{bad-{index}"},
        }
        for index in range(2)
    ]
    llm = _ScriptedLLM(
        [
            {"finish_reason": "tool_calls", "tool_calls": [bad_calls[0]]},
            {"finish_reason": "", "content": ""},  # 空输出轮（CONTINUE 重试）
            {"finish_reason": "tool_calls", "tool_calls": [bad_calls[1]]},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=10, temperature=0.2, max_tokens=1024, max_tool_protocol_retries=1
        ),
    ):
        pass

    # 若空输出轮清零了计数，第三轮只会重新计数为 1 并继续重试（calls > 3）
    assert llm.calls == 3
    assert strategy.outcome is not None
    assert "连续工具调用协议异常（2 轮）" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_protocol_hard_limit_preempts_same_round_business_failure():
    """协议预算耗尽时终局已定：同轮业务失败不再分发，其 RAISE 不得覆盖协议硬终止。"""
    dispatched: list[AgentErrorKind] = []

    async def on_tool_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        dispatched.append(ctx.kind)
        return AgentErrorAction.RAISE  # 若被调用则异常上抛，测试立即失败

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.TOOL_FAILED, on_tool_failed)

    bad_json = {
        "id": "bad",
        "type": "function",
        "function": {"name": "echo", "arguments": "{bad"},
    }
    business_fail = {
        "id": "fail",
        "type": "function",
        "function": {"name": "fail", "arguments": "{}"},
    }
    llm = _ScriptedLLM([{"finish_reason": "tool_calls", "tool_calls": [bad_json, business_fail]}])
    strategy = ReActStrategy(
        llm=llm,
        tools=_make_registry(tools=[_EchoTool(), _FailingTool()]),
        error_handlers=registry,
    )

    async for _ in strategy.execute(
        "hi",
        [{"role": "user", "content": "hi"}],
        **reasoning_execution_args(
            "react", max_iterations=3, temperature=0.2, max_tokens=1024, max_tool_protocol_retries=0
        ),
    ):
        pass

    assert dispatched == [], "协议硬上限已决定终局，同轮业务失败不应再分发"
    assert strategy.outcome is not None
    assert "连续工具调用协议异常（1 轮）" in (strategy.outcome.error or "")
