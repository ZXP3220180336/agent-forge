"""
PlannerStrategy 单元测试

覆盖：规划→执行→汇总成功路径 + 各降级路径（plan None→ReAct 兜底 / AppError→PLAN_FAILED /
步骤失败→replan / replan 耗尽→部分汇总 / summarize None→纯文本）+ PLAN_FAILED 分发
RAISE/STOP + schema 严格性 + done 抑制/口径一致 + usage 累计。

范式：手写假对象（不用 AsyncMock）——_PlannerLLM 脚本化 async_generate（每步 ReAct
小跑回填 StreamResult）与 generate_structured（plan/replan/summarize 返回 dict/None/抛异常）。
"""

import asyncio
import json

import pytest

from app.domain.reasoning import PlannerOutcome, PlannerStrategy
from app.domain.reasoning.planner import (
    PLAN_SCHEMA,
    PLAN_STEP_SCHEMA,
    REPLAN_SCHEMA,
    RESULT_SCHEMA,
)
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import build_message_event
from app.shared.exceptions import LLMAPIError

PLAN = {
    "goal": "分析批次 A 良率下降原因",
    "steps": [
        {"description": "查询批次 A 的日良率曲线，找出异常下降点", "depends_on": []},
        {"description": "基于良率曲线判断异常发生的具体日期", "depends_on": [1]},
    ],
}

REPLAN_TAIL = {
    "reason": "步骤 1 失败，改为直接查异常日志",
    "steps": [
        {"description": "查询批次 A 的异常日志定位下降原因", "depends_on": []},
    ],
}

SUMMARY = {
    "summary": "批次 A 良率下降因设备告警",
    "conclusions": [
        {
            "claim": "9/1 起良率下滑",
            "supporting_evidence": ["echo: query=yield"],
            "confidence": 0.8,
        }
    ],
    "next_steps": ["查设备告警明细"],
    "explicit_abstention": [],
}


class _PlannerLLM:
    """脚本化 LLM 替身：async_generate 回填 StreamResult（每步 ReAct），generate_structured 脚本返回。"""

    def __init__(
        self,
        react_scripts: list[dict],
        structured_scripts: list,
        usage: dict | None = None,
    ):
        self.react_scripts = react_scripts
        self.structured_scripts = structured_scripts
        self.usage_spec = usage or {}
        self.react_calls = 0
        self.structured_calls = 0

    async def async_generate(self, *args, result=None, **kwargs):
        self.react_calls += 1
        spec = self.react_scripts[
            min(self.react_calls - 1, len(self.react_scripts) - 1)
        ]
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield build_message_event(spec.get("content", ""))
        return

    async def generate_structured(
        self, messages, schema, model_key="fast", max_tokens=None, usage=None
    ):
        self.structured_calls += 1
        spec = self.structured_scripts[
            min(self.structured_calls - 1, len(self.structured_scripts) - 1)
        ]
        if isinstance(spec, Exception):
            raise spec
        if usage is not None and self.usage_spec:
            usage.update(self.usage_spec)
        return spec


def _typed(events: list[str], event_type: str) -> list[dict]:
    return [
        json.loads(e[len("data: ") :].strip())
        for e in events
        if e.startswith("data: ") and f'"type": "{event_type}"' in e
    ]


def _stop_script(content: str) -> dict:
    return {"finish_reason": "stop", "content": content}


async def _run(strategy, messages, **kw):
    events = []
    async for ev in strategy.execute(
        "分析批次 A 良率下降原因",
        messages,
        max_iterations=5,
        temperature=0.2,
        max_tokens=1024,
        **kw,
    ):
        events.append(ev)
    return events


# =====================================================================
# 成功路径
# =====================================================================


async def test_plan_execute_summarize_ok():
    """规划 → 2 步执行（第 2 步依赖第 1 步）→ 汇总 structured。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("良率曲线: 89%→80%"), _stop_script("异常日为 9/1")],
        structured_scripts=[PLAN, SUMMARY],
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "system", "content": "你是良率分析助手"}]

    events = await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.degraded is False
    assert strategy.outcome.structured == SUMMARY
    assert strategy.outcome.plan == {
        "goal": PLAN["goal"],
        "steps": [
            {"id": 1, "description": PLAN["steps"][0]["description"]},
            {"id": 2, "description": PLAN["steps"][1]["description"]},
        ],
    }
    assert [s["success"] for s in strategy.outcome.steps_executed] == [True, True]
    assert strategy.outcome.replan_rounds == 0
    assert llm.structured_calls == 2  # plan + summarize
    assert len(_typed(events, "done")) == 1  # 每步 ReAct done 被抑制，收尾仅 1


async def test_usage_accumulated_react_and_structured():
    """每步 react usage + plan/summarize structured usage 合并到 outcome。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("步骤1结果"), _stop_script("步骤2结果")],
        structured_scripts=[PLAN, SUMMARY],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages)

    assert strategy.outcome is not None
    # react 每步 spec 无 usage → react 累计 0；structured 2 次 × 15 = 30
    assert strategy.outcome.total_tokens == 30
    assert strategy.outcome.usage["total_tokens"] == 30
    done_evs = _typed(events, "done")
    assert done_evs[0]["total_tokens"] == 30  # done 口径与 outcome 一致


# =====================================================================
# 规划失败降级
# =====================================================================


async def test_plan_none_degrades_to_react():
    """plan 返回 None（结构化降级耗尽）→ 降级全量 ReAct 兜底。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("直接分析结果：设备 A 告警")],
        structured_scripts=[None],
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert strategy.outcome.success is True
    assert strategy.outcome.plan is None
    assert strategy.outcome.content == "直接分析结果：设备 A 告警"
    assert len(_typed(events, "done")) == 1


async def test_plan_apperror_degrades_to_react():
    """plan 抛 AppError（LLMAPIError 不可恢复）→ PLAN_FAILED 分发（默认 CONTINUE）→ ReAct 兜底。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("兜底结果")],
        structured_scripts=[LLMAPIError("401 认证失败", status_code=401)],
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert strategy.outcome.success is True
    assert "401" in strategy.outcome.error  # error 透传原 AppError 文本


async def test_plan_failed_raise_propagates():
    """PLAN_FAILED handler RAISE → 抛 AgentRunError（kind==PLAN_FAILED）。"""
    async def on_plan_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.PLAN_FAILED, on_plan_failed)
    llm = _PlannerLLM(
        react_scripts=[_stop_script("x")],
        structured_scripts=[LLMAPIError("boom")],
    )
    strategy = PlannerStrategy(llm=llm, tools=None, error_handlers=registry)
    messages = [{"role": "user", "content": "hi"}]

    with pytest.raises(AgentRunError) as exc_info:
        await _run(strategy, messages)
    assert exc_info.value.kind == AgentErrorKind.PLAN_FAILED


async def test_plan_failed_stop_marks_failure():
    """PLAN_FAILED handler STOP → success=False（硬失败标记，不做有效兜底）。"""
    async def on_plan_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.PLAN_FAILED, on_plan_failed)
    llm = _PlannerLLM(
        react_scripts=[_stop_script("兜底内容")],
        structured_scripts=[LLMAPIError("boom")],
    )
    strategy = PlannerStrategy(llm=llm, tools=None, error_handlers=registry)
    messages = [{"role": "user", "content": "hi"}]

    await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.degraded is True


# =====================================================================
# 步骤失败 → replan
# =====================================================================


async def test_step_fail_triggers_replan():
    """步骤 1 失败 → replan 产出修订尾 → 新步成功 → 汇总；失败步留审计。"""
    llm = _PlannerLLM(
        react_scripts=[
            {"error": "步骤1工具调用失败"},  # 步骤 1 失败（react LLM_FAILED）
            _stop_script("修订后：异常日志定位到设备 A"),
        ],
        structured_scripts=[PLAN, REPLAN_TAIL, SUMMARY],
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.structured == SUMMARY
    assert strategy.outcome.replan_rounds == 1
    assert strategy.outcome.steps_executed[0]["success"] is False  # 失败步留审计
    assert strategy.outcome.steps_executed[1]["success"] is True  # 修订步
    assert llm.structured_calls == 3  # plan + replan + summarize
    assert len(_typed(events, "done")) == 1


async def test_replan_exhausted_degrades_partial():
    """replan 返回 None（无法补救）→ 无成功步 → 纯失败 partial 降级。"""
    llm = _PlannerLLM(
        react_scripts=[{"error": "步骤失败"}],
        structured_scripts=[PLAN, None],  # plan 成功、replan 失败
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    await _run(strategy, messages, max_replan_rounds=1)

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert strategy.outcome.success is False
    assert "重规划" in strategy.outcome.error


# =====================================================================
# 汇总降级
# =====================================================================


async def test_summarize_none_plain_text():
    """汇总返回 None → 纯文本拼装降级。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("良率曲线: 89%→80%")],
        structured_scripts=[PLAN, None],  # plan 成功、summarize 失败
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert strategy.outcome.success is True  # 有步骤产出 → 纯文本部分成功
    assert strategy.outcome.structured is None
    assert "步骤 1" in strategy.outcome.content  # 纯文本含各步摘要


# =====================================================================
# Schema 严格性
# =====================================================================


async def test_schema_strict():
    """四个 schema：required + additionalProperties:False 强制。"""
    from jsonschema import validate

    assert PLAN_SCHEMA["required"] == ["goal", "steps"]
    assert PLAN_SCHEMA["additionalProperties"] is False
    assert PLAN_STEP_SCHEMA["required"] == ["description", "depends_on"]
    assert REPLAN_SCHEMA["required"] == ["reason", "steps"]
    assert RESULT_SCHEMA["required"] == [
        "summary",
        "conclusions",
        "next_steps",
        "explicit_abstention",
    ]
    # 正常 fixture 过校验
    validate(instance=PLAN, schema=PLAN_SCHEMA)
    validate(instance=REPLAN_TAIL, schema=REPLAN_SCHEMA)
    validate(instance=SUMMARY, schema=RESULT_SCHEMA)


# =====================================================================
# 护栏：取消 / 成本
# =====================================================================


async def test_cancel_after_plan_stops_partial():
    """规划成功后执行中取消 → 降级采用已完成步骤（若有）。"""
    cancel_event = asyncio.Event()

    class _CancelLLM(_PlannerLLM):
        async def async_generate(self, *args, result=None, **kwargs):
            cancel_event.set()  # 第一轮步骤 LLM 调用即取消
            async for e in super().async_generate(*args, result=result, **kwargs):
                yield e

    llm = _CancelLLM(
        react_scripts=[_stop_script("步骤结果")],
        structured_scripts=[PLAN, SUMMARY],
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, cancel_event=cancel_event)

    assert strategy.outcome is not None
    assert strategy.outcome.error == "用户取消，采用已完成步骤（部分进度）"


async def test_replan_new_steps_renumbered():
    """replan 新步 id 续接已执行最大 id 之后（防依赖错位）。"""
    llm = _PlannerLLM(
        react_scripts=[
            {"error": "步骤1失败"},
            _stop_script("修订成功"),
        ],
        structured_scripts=[PLAN, REPLAN_TAIL, SUMMARY],
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    await _run(strategy, messages)

    assert strategy.outcome is not None
    # 失败步 id=1（executed[0]）；修订新尾从 max id + 1 = 2 起
    ids = [s["id"] for s in strategy.outcome.steps_executed]
    assert ids == [1, 2]


class _OverCostLimiter:
    """成本恒超限的假 CostLimiterPort。"""

    def check(self, usage):
        return True, 1.23


async def test_cost_limit_stops_before_plan():
    """成本累计超限 → 未开始规划即停机降级。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("不应执行")],
        structured_scripts=[PLAN],
    )
    strategy = PlannerStrategy(llm=llm, tools=None, cost_limiter=_OverCostLimiter())
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert strategy.outcome.success is False
    assert "成本超限" in strategy.outcome.error
    assert llm.structured_calls == 0  # 未发起任何付费调用


class _CostCalcLLM:
    """结构实现 LLMGateway.calculate_cost（镜像 LLMService 静态代理 CostTracker）。"""

    @staticmethod
    def calculate_cost(usage, model=""):
        from app.integration.llm.cost_tracker import CostTracker

        return CostTracker.calculate(usage, model)


def _cost_limiter(ceiling: float) -> "object":
    from app.application.context.cost_limiter import CostLimiter

    return CostLimiter(ceiling=ceiling, llm=_CostCalcLLM(), model="gpt-4")


async def test_step_react_stops_on_accumulated_cost():
    """步骤 react 子跑带 planner 累计基线：子跑中途累计越界即在越界轮停，不空转 replan/summarize。

    数值（gpt-4）：plan 结构化 usage {400,100}=0.018；step1 react {100,100}=0.009
    → 0.027；step2 round1 {200,150}=0.015 → 基线+局部 0.042 > ceiling 0.04 → 子跑
    内部 COST_EXCEEDED（无 baseline 时子跑局部 0.015 不触发，会正常完成）→ 步骤失败
    → replan 被累计 guard 拦 → 部分降级收尾，仅 plan 一笔付费调用。
    """
    llm = _PlannerLLM(
        react_scripts=[
            {"finish_reason": "stop", "content": "步骤 1 结果",
             "usage": {"prompt_tokens": 100, "completion_tokens": 100}},
            # step2 子跑首轮即越界（成本检查先于空输出/stop 分支）→ 空 content 故步骤判失败
            {"finish_reason": "stop", "content": "",
             "usage": {"prompt_tokens": 200, "completion_tokens": 150}},
        ],
        structured_scripts=[PLAN, SUMMARY],
        usage={"prompt_tokens": 400, "completion_tokens": 100},
    )
    strategy = PlannerStrategy(llm=llm, tools=None, cost_limiter=_cost_limiter(0.04))
    messages = [{"role": "user", "content": "hi"}]

    await _run(strategy, messages)

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert "成本超限" in (strategy.outcome.error or "")
    assert strategy.outcome.success is True  # 有已完成步骤（部分进度）
    # 越界停：只发起 plan 一笔付费调用，未空转 replan / summarize
    assert llm.structured_calls == 1
    # 部分进度：step1 完成进入 executed
    assert [s["success"] for s in strategy.outcome.steps_executed] == [True, False]
