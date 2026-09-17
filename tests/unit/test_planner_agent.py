"""
PlannerAgent 桥接单元测试

覆盖：_map_outcome metadata 映射（plan/steps_executed/replan_rounds/degraded）/
ctx.max_refine_rounds→replan 预算 / 端到端 run / outcome None 兜底。

范式：手写假对象（不用 AsyncMock）——LLM 替身实现 async_generate（步骤小跑）+
generate_structured（plan/summarize）。
"""

import asyncio
import json

import pytest

from app.domain.agent import AgentContext, PlannerAgent
from app.domain.ports.llm_gateway import StreamResult
from app.shared.events import build_message_event

PLAN = {
    "goal": "分析批次 A 良率下降原因",
    "steps": [{"description": "查询批次 A 日良率曲线", "depends_on": []}],
}

SUMMARY = {
    "summary": "设备 A 告警致良率下降",
    "conclusions": [
        {
            "claim": "设备 A 告警",
            "supporting_evidence": ["echo: query=yield"],
            "confidence": 0.8,
        }
    ],
    "next_steps": [],
    "explicit_abstention": [],
}


class _AgentPlannerLLM:
    """PlannerAgent 测试替身：async_generate 回填 + generate_structured 脚本。"""

    def __init__(self, structured_scripts):
        self.structured_scripts = structured_scripts
        self.structured_calls = 0
        self.plan_messages = None

    async def async_generate(self, *args, result=None, **kwargs):
        if result is not None:
            result.finish_reason = "stop"
            result.content = "步骤结果"
        yield build_message_event("步骤结果")
        return

    async def generate_structured(
        self,
        messages,
        schema,
        model_key="fast",
        max_tokens=None,
        usage=None,
        cancel_event=None,
        deadline=None,
    ):
        self.structured_calls += 1
        if self.structured_calls == 1:
            self.plan_messages = messages
        spec = self.structured_scripts[min(self.structured_calls - 1, len(self.structured_scripts) - 1)]
        return spec


def _ctx(**kw) -> AgentContext:
    base = dict(
        session_id="sess_1",
        user_id="user_1",
        run_id="run-planner",
        run_stop=asyncio.Event(),
        max_iterations=5,
        temperature=0.2,
        max_tokens=1024,
    )
    base.update(kw)
    return AgentContext(**base)


async def test_planner_agent_end_to_end_metadata():
    """run 桥接：结构化产出 + plan/steps_executed/degraded 进 metadata。"""
    llm = _AgentPlannerLLM(structured_scripts=[PLAN, SUMMARY])
    agent = PlannerAgent(llm=llm, tools=None)
    ctx = _ctx()

    events = []
    async for ev in agent.run("分析批次 A 良率下降原因", [{"role": "user", "content": "hi"}], ctx):
        events.append(ev)

    assert agent.result is not None
    assert agent.result.success is True
    assert agent.result.structured == SUMMARY
    assert agent.result.metadata["plan"]["goal"] == PLAN["goal"]
    assert len(agent.result.metadata["steps_executed"]) == 1
    assert agent.result.metadata["steps_executed"][0]["success"] is True
    assert agent.result.metadata["replan_rounds"] == 0
    assert agent.result.metadata["degraded"] is False
    assert agent.result.content  # summary 摘要
    assert any('"type": "done"' in e for e in events)


async def test_map_outcome_none_returns_failure():
    """策略未产出 → 失败 AgentResult。"""
    llm = _AgentPlannerLLM(structured_scripts=[None])
    agent = PlannerAgent(llm=llm, tools=None)
    ctx = _ctx()
    # plan 返回 None → 降级 ReAct 兜底（react 产出）→ 仍 success；此处构造 outcome None 场景：
    # 直接测 _map_outcome(None) 分支
    result = agent._map_outcome(None)
    assert result.success is False
    assert "未产出结果" in result.error


async def test_ctx_max_refine_rounds_as_replan_budget():
    """ctx.max_refine_rounds 透传为 max_replan_rounds（复用语义）。"""

    class _FailStepLLM(_AgentPlannerLLM):
        async def async_generate(self, *args, result=None, **kwargs):
            if result is not None:
                result.error = "模拟失败"
            yield build_message_event("")
            return

    llm = _FailStepLLM(structured_scripts=[PLAN, None, SUMMARY])  # 步骤失败 → replan → replan None
    agent = PlannerAgent(llm=llm, tools=None)
    ctx = _ctx(max_refine_rounds=1)  # replan 预算 1

    async for _ in agent.run("任务", [{"role": "user", "content": "hi"}], ctx):
        pass

    assert agent.result is not None
    assert agent.result.success is False  # replan 1 次失败后无成功步 → 失败
    assert agent.result.metadata["degraded"] is True


async def test_context_max_tool_protocol_retries_reaches_planner_step_react():
    """PlannerAgent 必须把协议修正预算传给每步 ReAct 子跑。"""

    class _ProtocolErrorLLM(_AgentPlannerLLM):
        def __init__(self):
            super().__init__([PLAN])
            self.react_calls = 0

        async def async_generate(self, *args, result=None, **kwargs):
            self.react_calls += 1
            if result is not None:
                result.finish_reason = "tool_calls"
            yield build_message_event("")

    llm = _ProtocolErrorLLM()
    agent = PlannerAgent(llm=llm, tools=None)
    ctx = _ctx(max_refine_rounds=0, max_tool_protocol_retries=0)

    async for _ in agent.run("任务", [{"role": "user", "content": "hi"}], ctx):
        pass

    assert llm.react_calls == 1
    assert agent.result is not None
    assert agent.result.success is False
    assert agent.result.metadata["degraded"] is True
