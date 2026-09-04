"""
ReActStrategy 非流式通道（stream_mode=False）单元测试

覆盖（stream_mode=False 走 LLMGateway.generate() 一次拿完整 StreamResult）：
    单轮 stop 成功 → 合成整条 reasoning/message 事件 + model_key 断言
    工具循环（execute_tool_calls 复用）
    generate 返回 None（可恢复耗尽）→ LLM_FAILED（含 error 事件，非空输出）
    generate 成功返回 + 调用中 cancel 置位 → 轮末补查 CANCELLED
    generate 抛 AppError（LLMAPIError）→ LLM_FAILED（非 UNKNOWN）
    generate 抛非 AppError（RuntimeError）→ UNKNOWN（对齐流式现状）
    usage 跨轮累计
    空输出重试后恢复
    reasoning-only 终轮（0 message / 1 reasoning 事件）
    final_answer 结构化（非流式 output_schema 路径）
    stream_mode 参数化双通道：同一脚本两通道 outcome 一致

范式：手写假对象（不用 AsyncMock），LLM 替身实现 generate() 脚本化返回。
"""

import asyncio
import json

import pytest

from app.domain.ports.llm_gateway import StreamResult
from app.domain.reasoning import ReActStrategy
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.tool_service import ToolService
from app.shared.events import build_message_event
from app.shared.exceptions import LLMAPIError


class _NonStreamingScriptedLLM:
    """实现 generate() 的脚本 LLM 替身；记录调用参数供断言。

    scripts: list[dict | None | Exception]，耗尽复用最后一条：
        dict → 字段 setattr 到新 StreamResult 返回（成功）
        None → 返回 None（可恢复错误重试耗尽）
        Exception → raise（AppError → LLM_FAILED；其他 → UNKNOWN）
    """

    def __init__(self, scripts: list):
        self.scripts = scripts
        self.calls = 0
        self.last_kwargs: dict = {}

    async def generate(
        self,
        messages=None,
        tools=None,
        temperature=None,
        max_tokens=None,
        model_key=None,
        response_format=None,
    ) -> StreamResult | None:
        self.calls += 1
        self.last_kwargs = {
            "messages": messages,
            "tools": tools,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "model_key": model_key,
        }
        spec = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        if spec is None:
            return None
        if isinstance(spec, Exception):
            raise spec
        sr = StreamResult()
        for k, v in spec.items():
            setattr(sr, k, v)
        return sr


class _EchoTool(BaseTool):
    """即时返回的工具（验证非流式工具循环）。"""

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


def _make_registry(tools=None):
    reg = ToolService(max_concurrent_tools=4)
    for tool in tools or []:
        reg.register(tool)
    return reg


def _events_payloads(events: list[str]) -> list[dict]:
    """解析 SSE 事件字符串 → [{"type","content",...}]。"""
    out = []
    for e in events:
        if e.startswith("data: "):
            out.append(json.loads(e[len("data: ") :].strip()))
    return out


def _typed(events: list[str], event_type: str) -> list[dict]:
    return [p for p in _events_payloads(events) if p["type"] == event_type]


async def _run(strategy, messages, **kw):
    """收集 execute 全部事件（缺省护栏参数默认）。"""
    events = []
    async for ev in strategy.execute(
        "测试输入",
        messages,
        max_iterations=10,
        temperature=0.2,
        max_tokens=1024,
        **kw,
    ):
        events.append(ev)
    return events


# =====================================================================
# 成功路径：事件合成 + 通道参数
# =====================================================================


async def test_nonstream_single_stop_synthesizes_events():
    """单轮 stop（reasoning + content）→ 整条 reasoning 先、message 后；model_key=main。"""
    llm = _NonStreamingScriptedLLM(
        [{"finish_reason": "stop", "content": "答案", "reasoning_content": "推理"}]
    )
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "答案"
    assert strategy.outcome.reasoning == "推理"
    reasoning_evs = _typed(events, "reasoning")
    message_evs = _typed(events, "message")
    assert [p["content"] for p in reasoning_evs] == ["推理"]  # 整条一次性
    assert [p["content"] for p in message_evs] == ["答案"]
    payloads = _events_payloads(events)
    assert payloads.index(message_evs[0]) > payloads.index(reasoning_evs[0])  # 时序 reasoning 先
    assert _typed(events, "done")
    # generate 显式 main（对齐 async_generate 默认），temperature/max_tokens 透传
    assert llm.last_kwargs["model_key"] == "main"
    assert llm.last_kwargs["temperature"] == 0.2
    assert llm.last_kwargs["max_tokens"] == 1024
    assert llm.last_kwargs["tools"] is None


async def test_nonstream_tool_loop():
    """非流式工具循环：tool_calls → 执行工具 → 再 generate stop。"""
    llm = _NonStreamingScriptedLLM(
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
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert len(strategy.outcome.tool_calls) == 1
    assert strategy.outcome.tool_calls[0]["tool"] == "echo"
    assert strategy.outcome.tool_calls[0]["result"] == "echo:hi"
    assert any(m.get("role") == "tool" for m in messages)
    assert llm.calls == 2  # 两轮都走 generate
    assert _typed(events, "tool_call") and _typed(events, "tool_result")


# =====================================================================
# 失败路径映射
# =====================================================================


async def test_nonstream_none_maps_to_llm_failed():
    """generate 返回 None（可恢复耗尽）→ LLM_FAILED 终态（非空输出重试）。"""
    llm = _NonStreamingScriptedLLM([None])
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "可恢复错误重试耗尽" in strategy.outcome.error
    assert strategy.outcome.iterations == 1
    error_evs = _typed(events, "error")
    assert error_evs and "可恢复错误重试耗尽" in error_evs[0]["content"]
    assert _typed(events, "done")


async def test_nonstream_cancel_after_return_cancels():
    """成功返回 + 调用期间 cancel 置位 → 轮末补查 CANCELLED（非流式 generate 无 chunk 中断）。"""

    class _CancelOnFirstGenerate(_NonStreamingScriptedLLM):
        def __init__(self, scripts, cancel_event):
            super().__init__(scripts)
            self._cancel_event = cancel_event

        async def generate(self, **kwargs):
            sr = await super().generate(**kwargs)
            if self.calls == 1:
                self._cancel_event.set()  # 模拟 generate 返回时用户已取消
            return sr

    cancel_event = asyncio.Event()
    llm = _CancelOnFirstGenerate(
        [{"finish_reason": "stop", "content": "答案"}], cancel_event
    )
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(
        strategy, messages, stream_mode=False, cancel_event=cancel_event
    )

    assert strategy.outcome is not None
    assert strategy.outcome.error == "Agent 已被取消"  # 轮末补查 → CANCELLED
    assert strategy.outcome.content == "答案"  # 部分进度保留（取消语义，非丢弃）
    assert _typed(events, "done")


async def test_nonstream_apperror_maps_to_llm_failed():
    """generate 抛 AppError（LLMAPIError 不可恢复）→ LLM_FAILED（对齐流式整流，非 UNKNOWN）。"""
    llm = _NonStreamingScriptedLLM([LLMAPIError("401 认证失败", status_code=401)])
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "401 认证失败" in strategy.outcome.error
    assert "Agent 运行异常" not in strategy.outcome.error  # 非 UNKNOWN
    error_evs = _typed(events, "error")
    assert error_evs and "401 认证失败" in error_evs[0]["content"]
    assert _typed(events, "done")


async def test_nonstream_non_apperror_maps_to_unknown():
    """generate 抛非 AppError（RuntimeError）→ 冒泡外层 except → UNKNOWN。"""
    llm = _NonStreamingScriptedLLM([RuntimeError("boom")])
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "RuntimeError" in strategy.outcome.error  # UNKNOWN 脱敏为异常类型名
    assert _typed(events, "done")


# =====================================================================
# 护栏语义
# =====================================================================


async def test_nonstream_usage_accumulates_across_rounds():
    """两轮（tool_calls→stop）各带 usage → outcome 跨轮累计。"""
    llm = _NonStreamingScriptedLLM(
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
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
            {
                "finish_reason": "stop",
                "content": "完成",
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=_make_registry(tools=[_EchoTool()]))
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.total_tokens == 45  # 15 + 30
    assert strategy.outcome.usage["prompt_tokens"] == 30
    assert strategy.outcome.usage["completion_tokens"] == 15
    done_evs = _typed(events, "done")
    assert done_evs[0]["total_tokens"] == 45


async def test_nonstream_empty_output_retries_then_stops():
    """空输出轮 → 重试；第 2 轮 stop 恢复。"""
    llm = _NonStreamingScriptedLLM([{}, {"finish_reason": "stop", "content": "完成"}])
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "完成"
    assert llm.calls == 2  # 两轮都走 generate


async def test_nonstream_reasoning_only_final_round():
    """reasoning-only 终轮（content 空 + stop）→ 1 reasoning 事件、0 message 事件。"""
    llm = _NonStreamingScriptedLLM(
        [{"finish_reason": "stop", "content": "", "reasoning_content": "仅思考"}]
    )
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False  # content 空
    assert strategy.outcome.reasoning == "仅思考"
    assert not _typed(events, "message")
    reasoning_evs = _typed(events, "reasoning")
    assert [p["content"] for p in reasoning_evs] == ["仅思考"]


async def test_nonstream_final_answer_structured():
    """final_answer 工具（非流式 output_schema 路径）→ 结构化提取终止。"""
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    llm = _NonStreamingScriptedLLM(
        [
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": json.dumps({"answer": "42"}),
                        },
                    }
                ],
            }
        ]
    )
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=False, output_schema=schema)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.structured == {"answer": "42"}
    assert not strategy.outcome.tool_calls  # final_answer 非真实工具，不进证据链
    assert _typed(events, "done")


# =====================================================================
# 双通道一致性：同一脚本，stream_mode 只换通道
# =====================================================================


class _DualLLM:
    """同时实现 async_generate（流式）与 generate（非流式），共享同一脚本。"""

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

    async def generate(self, **kwargs):
        self.calls += 1
        spec = self.scripts[min(self.calls - 1, len(self.scripts) - 1)]
        sr = StreamResult()
        for key, value in spec.items():
            setattr(sr, key, value)
        return sr


@pytest.mark.parametrize("stream_mode", [True, False])
async def test_dual_channel_same_outcome(stream_mode):
    """同一脚本：流式/非流式通道产出同一 outcome（单循环只换通道）。"""
    llm = _DualLLM([{"finish_reason": "stop", "content": "答案"}])
    strategy = ReActStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, stream_mode=stream_mode)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.content == "答案"
    # 两通道 message 内容一致（流式=整条脚本事件 / 非流式=合成整条）
    assert [p["content"] for p in _typed(events, "message")] == ["答案"]
    assert _typed(events, "done")
