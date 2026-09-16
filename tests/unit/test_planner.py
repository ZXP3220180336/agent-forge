"""
PlannerStrategy 单元测试

覆盖：规划→执行→汇总成功路径 + 各降级路径（plan None→ReAct 兜底 / AppError→PLAN_FAILED /
步骤失败→replan / replan 耗尽→部分汇总 / summarize None→纯文本）+ PLAN_FAILED 分发
RAISE/STOP + schema 严格性 + done 抑制/口径一致 + usage 累计。

范式：手写假对象（不用 AsyncMock）——_PlannerLLM 脚本化 async_generate（每步 ReAct
小跑回填 StreamResult）与 generate_structured（plan/replan/summarize 返回 dict/None/抛异常）。
"""

import asyncio
import copy
import json
import time

import pytest

from app.domain.reasoning import PlannerOutcome, PlannerStrategy
from app.domain.reasoning._planner_steps import (
    build_plan_payload,
    build_step_record,
    normalize_steps,
)
from app.domain.reasoning.planner import (
    PLAN_SCHEMA,
    PLAN_STEP_SCHEMA,
    REPLAN_SCHEMA,
    RESULT_SCHEMA,
)
from app.domain.reasoning.react import ReActOutcome
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import build_message_event
from app.shared.exceptions import ContextWindowExceededError, LLMAPIError
from tests.reasoning_execution import reasoning_execution_args

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
        spec = self.structured_scripts[
            min(self.structured_calls - 1, len(self.structured_scripts) - 1)
        ]
        if isinstance(spec, Exception):
            raise spec
        if usage is not None and self.usage_spec:
            usage.update(self.usage_spec)
        return spec


class _SelectiveContextBudget:
    """只控制结构化阶段计数；消息轮次裁剪不属于本组测试目标。"""

    def __init__(self, oversized_marker: str | None = None) -> None:
        self.oversized_marker = oversized_marker

    def count_tokens(self, text: str) -> int:
        if self.oversized_marker and self.oversized_marker not in text:
            return 10
        return len(text)

    def trim_messages(self, messages, *, max_rounds, max_tokens) -> None:
        return None


def _typed(events: list[str], event_type: str) -> list[dict]:
    return [
        json.loads(e[len("data: ") :].strip())
        for e in events
        if e.startswith("data: ") and f'"type": "{event_type}"' in e
    ]


def _stop_script(content: str) -> dict:
    return {"finish_reason": "stop", "content": content}


async def _run(strategy, messages, **kw):
    kw.setdefault("run_id", "run-planner-test")
    kw.setdefault("run_stop", asyncio.Event())
    events = []
    async for ev in strategy.execute(
        "分析批次 A 良率下降原因",
        messages,
        **reasoning_execution_args(
            "planner",
            max_iterations=5,
            temperature=0.2,
            max_tokens=1024,
            **kw,
        ),
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


async def test_cancel_after_successful_summary_keeps_summary_and_usage():
    """汇总成功返回时取消命中，先接管结构化结果与 usage 再终止。"""
    cancel_event = asyncio.Event()

    class _CancelAfterSummaryLLM(_PlannerLLM):
        async def generate_structured(self, *args, **kwargs):
            result = await super().generate_structured(*args, **kwargs)
            if self.structured_calls == 2:
                cancel_event.set()
            return result

    llm = _CancelAfterSummaryLLM(
        react_scripts=[_stop_script("步骤结果")],
        structured_scripts=[PLAN, SUMMARY],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    strategy = PlannerStrategy(llm=llm, tools=None)

    events = await _run(
        strategy,
        [{"role": "user", "content": "hi"}],
        cancel_event=cancel_event,
    )

    assert strategy.outcome is not None
    assert strategy.outcome.structured == SUMMARY
    assert strategy.outcome.content == SUMMARY["summary"]
    assert strategy.outcome.usage == {
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "total_tokens": 30,
    }
    assert "用户取消" in (strategy.outcome.error or "")
    assert len(_typed(events, "done")) == 1


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
    assert llm.react_calls == 0, "STOP 已选定终态后不得再启动 ReAct fallback"


async def test_plan_context_overflow_stops_without_react_fallback():
    """最终 payload 上下文超限时，未缩减同一请求不得转普通 PLAN_FAILED fallback。"""
    overflow = ContextWindowExceededError(
        model_key="fast", input_tokens=120, input_budget=100, max_tokens=20
    )
    llm = _PlannerLLM(
        react_scripts=[_stop_script("不应执行")], structured_scripts=[overflow]
    )
    strategy = PlannerStrategy(llm=llm, tools=None)

    await _run(strategy, [{"role": "user", "content": "hi"}])

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.degraded is True
    assert "上下文超限" in (strategy.outcome.error or "")
    assert llm.react_calls == 0


async def test_plan_minimal_prompt_overflow_stops_before_structured_call():
    """Domain 最小规划骨架超限时不把已知非法请求交给 Integration。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("不应执行")], structured_scripts=[PLAN]
    )
    strategy = PlannerStrategy(
        llm=llm, tools=None, context_budget=_SelectiveContextBudget()
    )

    await _run(
        strategy,
        [{"role": "user", "content": "hi"}],
        max_context_tokens=1,
    )

    assert llm.structured_calls == 0
    assert llm.react_calls == 0
    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "上下文超限" in (strategy.outcome.error or "")


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


async def test_replan_minimal_prompt_overflow_keeps_failed_step_without_new_call():
    """重规划本地超限保留失败事实，且只有失败记录不能标为部分成功。"""
    llm = _PlannerLLM(
        react_scripts=[{"error": "步骤1工具调用失败"}],
        structured_scripts=[PLAN],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    strategy = PlannerStrategy(
        llm=llm,
        tools=None,
        context_budget=_SelectiveContextBudget("某一步执行失败"),
    )

    await _run(
        strategy,
        [{"role": "user", "content": "hi"}],
        max_context_tokens=50,
        max_replan_rounds=1,
    )

    assert llm.structured_calls == 1
    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.plan is not None
    assert strategy.outcome.steps_executed[0]["success"] is False
    assert strategy.outcome.steps_executed[0]["depends_on"] == []
    assert strategy.outcome.total_tokens == 15


async def test_summarize_minimal_prompt_overflow_keeps_completed_steps_and_usage():
    """汇总本地超限不丢已完成步骤，并避免发出第二笔结构化请求。"""
    llm = _PlannerLLM(
        react_scripts=[_stop_script("结果一"), _stop_script("结果二")],
        structured_scripts=[PLAN],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    strategy = PlannerStrategy(
        llm=llm,
        tools=None,
        context_budget=_SelectiveContextBudget("报告撰写者"),
    )

    await _run(
        strategy,
        [{"role": "user", "content": "hi"}],
        max_context_tokens=50,
    )

    assert llm.structured_calls == 1
    assert strategy.outcome is not None
    assert strategy.outcome.success is True
    assert strategy.outcome.degraded is True
    assert len(strategy.outcome.steps_executed) == 2
    assert strategy.outcome.total_tokens == 15
    assert "上下文超限" in (strategy.outcome.error or "")


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


async def test_replan_failed_stop_does_not_start_partial_summary():
    """replan handler 已选择 STOP 后不得再发起付费汇总。"""

    async def on_plan_failed(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry = ErrorHandlerRegistry()
    registry.register(AgentErrorKind.PLAN_FAILED, on_plan_failed)
    llm = _PlannerLLM(
        react_scripts=[_stop_script("步骤 1 成果"), {"error": "步骤 2 失败"}],
        structured_scripts=[PLAN, LLMAPIError("replan failed"), SUMMARY],
    )
    strategy = PlannerStrategy(llm=llm, tools=None, error_handlers=registry)

    await _run(strategy, [{"role": "user", "content": "hi"}])

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert strategy.outcome.success is True  # 已完成步骤仍是可交付的部分成果
    assert llm.structured_calls == 2, "STOP 后不得调用 summarize"


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
    """规划成功后执行中取消 → 降级采用已完成步骤（若有）。

    步骤判据是二维（react success 且产出非空），被取消的子跑只要有产出即记 success=True；
    sub.error 只记录停机原因、不参与判据，故该步的 error 记录为 None。
    """
    cancel_event = asyncio.Event()

    class _CancelLLM(_PlannerLLM):
        async def async_generate(self, *args, result=None, **kwargs):
            cancel_event.set()  # 第一轮步骤 LLM 调用即取消
            async for e in super().async_generate(*args, result=result, **kwargs):
                yield e

    llm = _CancelLLM(
        react_scripts=[
            {
                "finish_reason": "stop",
                "content": "步骤结果",
                "usage": {
                    "prompt_tokens": 6,
                    "completion_tokens": 2,
                    "total_tokens": 8,
                },
            }
        ],
        structured_scripts=[PLAN, SUMMARY],
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, cancel_event=cancel_event)

    assert strategy.outcome is not None
    assert strategy.outcome.error == "用户取消，采用已完成步骤（部分进度）"
    assert strategy.outcome.usage == {
        "prompt_tokens": 6,
        "completion_tokens": 2,
        "total_tokens": 8,
    }
    assert len(strategy.outcome.steps_executed) == 1
    assert strategy.outcome.steps_executed[0]["success"] is True
    assert strategy.outcome.steps_executed[0]["content"] == "步骤结果"
    assert len(_typed(events, "done")) == 1


async def test_guard_after_plan_returns_contract_plan_snapshot():
    """规划后护栏命中 → plan 仍为契约形状快照 {goal, steps:[{id, description}]}。

    触发：plan 这笔付费调用返回后立即置位 cancel_event（模拟运行中用户取消）→ 段首护栏
    拦下，plan is not None 分支生效（文案「规划后中止，未开始执行」），零步骤 ReAct 调用。
    断言重点：快照由 normalize_steps 赋 id、不含 depends_on（raw plan 的 depends_on 只作
    顺序纪律断言），与 plan_result 同源——消费方 PlannerAgent.metadata["plan"] 口径唯一。
    """
    cancel_event = asyncio.Event()

    class _CancelAfterPlanLLM(_PlannerLLM):
        async def generate_structured(self, *args, **kwargs):
            result = await super().generate_structured(*args, **kwargs)
            if self.structured_calls == 1:  # plan 归账后取消（第 2 笔尚未发起）
                cancel_event.set()
            return result

    llm = _CancelAfterPlanLLM(
        react_scripts=[_stop_script("不应执行")],
        structured_scripts=[PLAN],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    strategy = PlannerStrategy(llm=llm, tools=None)
    messages = [{"role": "user", "content": "hi"}]

    events = await _run(strategy, messages, cancel_event=cancel_event)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert strategy.outcome.degraded is True
    assert strategy.outcome.error == "用户取消，规划后中止，未开始执行"
    assert strategy.outcome.plan == {
        "goal": PLAN["goal"],
        "steps": [
            {"id": 1, "description": PLAN["steps"][0]["description"]},
            {"id": 2, "description": PLAN["steps"][1]["description"]},
        ],
    }
    assert all("depends_on" not in s for s in strategy.outcome.plan["steps"])
    assert strategy.outcome.steps_executed == []
    assert llm.structured_calls == 1  # 仅 plan 一笔付费调用（未 summarize）
    assert llm.react_calls == 0  # 未发起任何步骤 ReAct
    assert strategy.outcome.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }
    assert len(_typed(events, "done")) == 1


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


# ── E：结构化调用取消/期限信号下沉（planner 侧接线）──


async def test_planner_passes_cancel_deadline_to_structured():
    """E：execute 的 cancel_event 与总时长现算 deadline 透传到规划结构化调用。

    防线：planner 各阶段入口 guard 外，plan/summarize 的 generate_structured 必须
    拿到同一 cancel_event / 绝对 deadline，结构化内部才能拦截降级链（G1 接线验证）。
    """
    llm = _PlannerLLM(
        react_scripts=[_stop_script("步骤1结果"), _stop_script("步骤2结果")],
        structured_scripts=[PLAN, SUMMARY],
    )
    captured = {}
    orig = llm.generate_structured

    async def rec(messages, schema, model_key="fast", max_tokens=None, usage=None, cancel_event=None, deadline=None):
        if not captured:  # 首笔结构化调用 = plan
            captured["cancel_event"] = cancel_event
            captured["deadline"] = deadline
        return await orig(
            messages, schema, model_key=model_key, max_tokens=max_tokens,
            usage=usage, cancel_event=cancel_event, deadline=deadline,
        )

    llm.generate_structured = rec
    cancel_event = asyncio.Event()
    before = time.monotonic()
    strategy = PlannerStrategy(llm=llm, tools=None)
    await _run(
        strategy,
        [{"role": "user", "content": "hi"}],
        max_execution_time=60.0,
        cancel_event=cancel_event,
    )

    assert captured["cancel_event"] is cancel_event, "规划收到同一取消信号"
    assert captured["deadline"] is not None, "传了 max_execution_time 时 deadline 应非 None"
    assert before + 60.0 <= captured["deadline"] <= time.monotonic() + 60.0


def test_normalize_steps_keeps_ids_dependencies_and_input_immutable():
    raw_steps = [
        {"description": " first ", "depends_on": [1, 4, 5]},
        {"description": "second", "depends_on": [2, 5, 9]},
        {"description": "   ", "depends_on": []},
    ]
    original = copy.deepcopy(raw_steps)

    normalized = normalize_steps(raw_steps, completed={1, 2}, start_no=5)

    assert normalized == [
        {"id": 5, "description": "first", "deps": {1}},
        {"id": 6, "description": "second", "deps": {2, 5}},
    ]
    assert raw_steps == original


def test_build_plan_payload_hides_internal_dependencies():
    steps = [{"id": 3, "description": "collect", "deps": {1, 2}}]

    assert build_plan_payload("goal", steps) == {
        "goal": "goal",
        "steps": [{"id": 3, "description": "collect"}],
    }


def test_build_step_record_preserves_two_dimensional_success_contract():
    outcome = ReActOutcome(
        content="x" * 600,
        tool_calls=[{"id": "call-1"}],
        iterations=3,
        total_tokens=21,
        error="达到迭代上限",
        success=True,
    )

    record = build_step_record(
        {"id": 2, "description": "collect", "deps": {1}},
        outcome,
    )

    assert record["success"] is True
    assert record["error"] is None
    assert record["summary"] == "x" * 500
    assert record["content"] == "x" * 600
    assert record["depends_on"] == [1]
    assert record["tool_calls"] == [{"id": "call-1"}]
    assert record["iterations"] == 3
    assert record["total_tokens"] == 21


@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [
        (None, "ReAct 子跑未产出结果"),
        (ReActOutcome(content="", success=True), "步骤产出为空"),
    ],
)
def test_build_step_record_handles_missing_or_empty_outcome(outcome, expected_error):
    record = build_step_record(
        {"id": 1, "description": "collect", "deps": set()},
        outcome,
    )

    assert record["success"] is False
    assert record["error"] == expected_error
