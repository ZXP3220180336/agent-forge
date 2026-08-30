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

import pytest

from app.config import settings
from app.domain.reasoning import ReActStrategy
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.tool_service import ToolService
from app.shared.error_handling import (
    AgentRunError,
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    ErrorHandlerRegistry,
)
from app.shared.events import build_error_event, build_message_event


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
        return


class _ErrorLLM:
    """模拟 LLM 失败：产出一个 SSE error 事件，并在 result 上标记 error（LLM-001）。"""

    async def async_generate(self, *args, result=None, **kwargs):
        if result is not None:
            result.error = "401 认证失败"
        yield build_error_event("LLM 调用失败: 401 认证失败")
        return


class _EmptyLLM:
    """每轮返回空输出（finish_reason 为空），用于触发重试 / 迭代兜底。"""

    async def async_generate(self, *args, result=None, **kwargs):
        if result is not None:
            result.finish_reason = ""
            result.content = ""
        yield ""
        return


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
        return


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
        "30C 转华氏", messages, max_iterations=3, temperature=0.2, max_tokens=1024
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert strategy.outcome.iterations == 2


@pytest.mark.asyncio
async def test_react_execute_max_iterations_fallback():
    """持续空输出 → 达到 max_iterations 强制结束（用 last_result 兜底）。"""
    strategy = ReActStrategy(llm=_EmptyLLM(), tools=None)

    async for _ in strategy.execute(
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=2, temperature=0.2, max_tokens=1024,
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.iterations == 2
    assert strategy.outcome.success is False


@pytest.mark.asyncio
async def test_react_execute_timeout_first_iteration():
    """首轮 LLM 调用即超时 → 降级 outcome：success=False + error 记录超时 + iterations=1。"""
    strategy = ReActStrategy(llm=_SleepyLLM(sleep_before_call=1, delay=0.3), tools=None)

    events = []
    async for ev in strategy.execute(
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
        max_execution_time=0.05,
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
        delay=0.3,
    )
    tools = _make_registry(tools=[_EchoTool()])
    strategy = ReActStrategy(llm=llm, tools=tools)

    async for _ in strategy.execute(
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
        max_execution_time=0.05,
    ):
        pass

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.iterations == 2
    # 第 1 轮工具已执行完成，调用记录保留（部分进度证据）
    assert len(strategy.outcome.tool_calls) == 1
    assert strategy.outcome.tool_calls[0]["tool"] == "echo"
    assert "超时" in (strategy.outcome.error or "")


@pytest.mark.asyncio
async def test_react_execute_loose_timeout_does_not_trigger():
    """宽松时间上限不影响正常完成。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    async for _ in strategy.execute(
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
        max_execution_time=5.0,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
        max_execution_time=None,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
            {"finish_reason": "stop", "content": "完成",
             "usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None, cost_limiter=_cost_limiter(ceiling=10.0))

    async for _ in strategy.execute(
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
    strategy = ReActStrategy(
        llm=llm, tools=None, error_handlers=registry, cost_limiter=_cost_limiter()
    )

    with pytest.raises(AgentRunError) as exc:
        async for _ in strategy.execute(
            "hi", [{"role": "user", "content": "hi"}],
            max_iterations=3, temperature=0.2, max_tokens=1024,
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
    strategy = ReActStrategy(
        llm=llm, tools=None, error_handlers=registry, cost_limiter=_cost_limiter()
    )

    async for _ in strategy.execute(
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
    ):
        pass

    # CONTINUE 不改变终结性护栏：outcome 已设置（STOP 组装），循环终止
    assert strategy.outcome is not None
    assert strategy.outcome.iterations == 1
    assert "成本超限" in (strategy.outcome.error or "")


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
        "hi", messages, max_iterations=3, temperature=0.2, max_tokens=1024
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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

    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1):
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

    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1):
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
        "hi", messages, max_iterations=3, temperature=0.2, max_tokens=1024
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
        "hi", messages, max_iterations=3, temperature=0.2, max_tokens=1024
    ):
        pass

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "[结果已截断]" not in tool_msgs[0]["content"]
    assert tool_msgs[0]["content"] == "echo:hi"


@pytest.mark.asyncio
async def test_react_reasoning_feedback_when_has_reasoning():
    """has_reasoning=True 且 reasoning_content 空 → assistant 消息仍带 reasoning_content 字段（空串，DeepSeek V4 必须回喂）。"""
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
        "hi", messages, max_iterations=3, temperature=0.2, max_tokens=1024
    ):
        pass

    # 第 1 轮 assistant 消息（含 tool_calls）应带 reasoning_content 键（空串）
    first_assistant = next(
        m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    )
    assert "reasoning_content" in first_assistant
    assert first_assistant["reasoning_content"] == ""


@pytest.mark.asyncio
async def test_react_reasoning_no_feedback_without_signal():
    """无 has_reasoning（chat 模型）→ assistant 消息不带 reasoning_content 键。"""
    llm = _ScriptedLLM([{"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)

    messages = [{"role": "user", "content": "hi"}]
    async for _ in strategy.execute(
        "hi", messages, max_iterations=3, temperature=0.2, max_tokens=1024
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
        "hi", messages, max_iterations=4, temperature=0.2, max_tokens=1024,
        max_context_rounds=2,
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
        "hi", messages, max_iterations=6, temperature=0.2, max_tokens=1024,
        max_context_rounds=2,
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
                            "arguments": json.dumps(
                                {"conclusion": "根因A", "confidence": 0.9}
                            ),
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
        "hi", messages, max_iterations=3, temperature=0.2, max_tokens=1024,
        output_schema=_FA_REPORT_SCHEMA,
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
        "hi", messages, max_iterations=3, temperature=0.2, max_tokens=1024,
        output_schema=_FA_REPORT_SCHEMA,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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

    strategy = ReActStrategy(
        llm=_ErrorLLM(), tools=None, error_handlers=registry
    )

    with pytest.raises(AgentRunError) as exc_info:
        async for _ in strategy.execute(
            "hi", [{"role": "user", "content": "hi"}],
            max_iterations=3, temperature=0.2, max_tokens=1024,
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

    strategy = ReActStrategy(
        llm=_EmptyLLM(), tools=None, error_handlers=registry
    )

    async for _ in strategy.execute(
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
        output_schema=_FA_REPORT_SCHEMA,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
        "hi", [{"role": "user", "content": "hi"}],
        max_iterations=3, temperature=0.2, max_tokens=1024,
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
            "hi", [{"role": "user", "content": "hi"}],
            max_iterations=3, temperature=0.2, max_tokens=1024,
        ):
            pass

    assert exc_info.value.kind == AgentErrorKind.PARSE_FAILED


@pytest.mark.asyncio
async def test_react_execute_tool_calls_parallel_preserves_order(monkeypatch):
    """execute_tool_calls：tool_messages 顺序保持 = tool_calls 输入顺序。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 10)
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

    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1):
        pass

    # tool_messages 顺序 = 输入顺序（gather 保序）
    assert [m["tool_call_id"] for m in messages] == ["call_1", "call_2", "call_3"]
    assert messages[0]["content"] == "tool_a:x1"
    assert messages[1]["content"] == "tool_b:x2"
    assert messages[2]["content"] == "tool_c:x3"


@pytest.mark.asyncio
async def test_react_execute_tool_calls_actually_concurrent(monkeypatch):
    """execute_tool_calls：并行执行总耗时 < 串行和。"""
    monkeypatch.setattr(settings, "agent_max_concurrent_tools", 10)
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
    async for _ in strategy.execute_tool_calls(tool_calls, messages, iteration=1):
        pass
    elapsed = time.monotonic() - start

    # 并行执行两个 0.05s 工具，总耗时应 < 串行 0.1s（留余量，断言 < 0.09）
    assert elapsed < 0.09, f"应并行执行（<0.09s），实际 {elapsed:.3f}s"


class _NoopLLM:
    """最小 LLM 替身（execute_tool_calls 不真正调用 LLM）。"""

    async def async_generate(self, *args, **kwargs):
        yield ""
        return
