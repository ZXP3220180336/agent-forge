"""
ReflectionAgent 桥接单元测试

覆盖：_map_outcome 字段映射 / ctx.max_refine_rounds 透传 / 桥接端到端三阶段 /
AgentContext 新字段默认向后兼容。

范式：复用 test_reflection.py 的 _ReflectionLLM / _EchoTool（手写假对象）。
"""

import json

import pytest

from app.domain.agent import AgentContext, ReflectionAgent
from app.domain.reasoning import ReflectionOutcome
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.tool_service import ToolService
from app.shared.events import build_message_event

DRAFT = {
    "summary": "良率下降归因于设备 A 告警",
    "conclusions": [
        {
            "claim": "设备 A 存在多次告警",
            "supporting_evidence": ["echo: query=alerts"],
            "confidence": 0.8,
        }
    ],
    "next_steps": ["查询设备 A 详细告警记录"],
    "explicit_abstention": [],
}


class _EchoTool(BaseTool):
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


class _ReflectionLLM:
    """脚本化 LLM 替身：async_generate 回填 StreamResult，generate_structured 脚本返回。"""

    def __init__(self, react_scripts: list[dict], structured_scripts: list):
        self.react_scripts = react_scripts
        self.structured_scripts = structured_scripts
        self.react_calls = 0
        self.structured_calls = 0

    async def async_generate(self, *args, result=None, **kwargs):
        self.react_calls += 1
        spec = self.react_scripts[min(self.react_calls - 1, len(self.react_scripts) - 1)]
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield build_message_event(spec.get("content", ""))
        return

    async def generate_structured(self, messages, schema, model_key="fast", max_tokens=None):
        self.structured_calls += 1
        spec = self.structured_scripts[
            min(self.structured_calls - 1, len(self.structured_scripts) - 1)
        ]
        if isinstance(spec, Exception):
            raise spec
        return spec


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _react_scripts_with_draft() -> list[dict]:
    return [
        {
            "finish_reason": "tool_calls",
            "tool_calls": [_tool_call("echo", {"text": "query=alerts"})],
        },
        {
            "finish_reason": "tool_calls",
            "tool_calls": [_tool_call("final_answer", DRAFT)],
        },
    ]


def _make_agent(llm) -> ReflectionAgent:
    tools = ToolService(max_concurrent_tools=10)
    tools.register(_EchoTool())
    return ReflectionAgent(llm=llm, tools=tools)


@pytest.mark.asyncio
async def test_map_outcome_maps_fields_to_result():
    """_map_outcome：structured/tool_calls 直接映射，draft/critique/degraded 进 metadata。"""
    agent = _make_agent(_ReflectionLLM([], []))
    outcome = ReflectionOutcome(
        content="",
        structured=DRAFT,
        draft=DRAFT,
        critique={"ok": False, "issues": []},
        refine_rounds=1,
        degraded=False,
        tool_calls=[{"tool": "echo", "params": {"text": "x"}}],
        iterations=2,
        total_tokens=100,
        success=True,
    )
    result = agent._map_outcome(outcome)

    assert result.success is True
    assert result.structured == DRAFT
    assert result.tool_calls[0]["tool"] == "echo"
    assert result.metadata["draft"] == DRAFT
    assert result.metadata["critique"]["ok"] is False
    assert result.metadata["refine_rounds"] == 1
    assert result.metadata["degraded"] is False


@pytest.mark.asyncio
async def test_map_outcome_none_returns_failure():
    """_map_outcome None → 失败 AgentResult。"""
    agent = _make_agent(_ReflectionLLM([], []))
    result = agent._map_outcome(None)
    assert result.success is False
    assert "未产出结果" in result.error


@pytest.mark.asyncio
async def test_context_max_refine_rounds_passthrough():
    """ctx.max_refine_rounds=1 → 自查 issues 但不修正（0 次 refine），验证透传。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺信号"}]}
        ],
    )
    agent = _make_agent(llm)
    ctx = AgentContext(session_id="s", user_id="u", max_refine_rounds=1)

    async for _ in agent.run("分析良率", [{"role": "user", "content": "分析良率"}], ctx):
        pass

    assert llm.structured_calls == 1  # 只有自查，无修正（max_refine_rounds=1 透传生效）
    assert agent.result is not None
    assert agent.result.structured == DRAFT


@pytest.mark.asyncio
async def test_reflection_agent_end_to_end_three_stages():
    """ReflectionAgent.run 桥接端到端：收集→初稿→自查 ok→采用。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(),
        structured_scripts=[{"ok": True, "issues": []}],
    )
    agent = _make_agent(llm)
    ctx = AgentContext(session_id="s", user_id="u")

    events = []
    async for event in agent.run("分析良率", [{"role": "user", "content": "分析良率"}], ctx):
        events.append(event)

    assert agent.result is not None
    assert agent.result.success is True
    assert agent.result.structured == DRAFT
    assert agent.result.metadata["degraded"] is False
    assert agent.result.metadata["refine_rounds"] == 0
    assert any('"type": "done"' in e for e in events)
    assert any("自查通过" in e for e in events)


def test_context_max_refine_rounds_default_backward_compatible():
    """AgentContext 新字段默认向后兼容（不传 = 2）。"""
    ctx = AgentContext(session_id="s", user_id="u")
    assert ctx.max_refine_rounds == 2
