"""
ReflectionStrategy 单元测试

覆盖：三阶段（收集+初稿 → 自查 → 修正）全路径 + 各降级路径 + Grounding 注入 +
迭代上限 + CRITIQUE_FAILED 分发 + 护栏透传。

范式：手写假对象（不用 AsyncMock）——_ReflectionLLM 脚本化 async_generate（回填
StreamResult）与 generate_structured（返回 dict/None/抛异常），记录入参供断言。
"""

import asyncio
import json

import pytest

from app.domain.reasoning import ReflectionOutcome, ReflectionStrategy
from app.domain.reasoning.reflection import CRITIQUE_SCHEMA, REFLECTION_SCHEMA
from app.domain.prompts.templates.reflection import CRITIQUE_PROMPT
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.tool_service import ToolService
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import build_message_event
from app.shared.exceptions import LLMAPIError, NonRetryableError, StructuredRefusalError

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

REFINED = {
    "summary": "良率下降归因于设备 A 告警（修正版）",
    "conclusions": [
        {
            "claim": "设备 A 告警与良率下降时间吻合",
            "supporting_evidence": ["echo: query=alerts", "echo: query=yield"],
            "confidence": 0.85,
        }
    ],
    "next_steps": ["查询设备 A 详细告警记录", "交叉验证历史类似 excursion"],
    "explicit_abstention": [],
}


class _EchoTool(BaseTool):
    """即时返回的工具（ReAct 收集阶段执行）。"""

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

    def __init__(self, react_scripts: list[dict], structured_scripts: list, usage: dict | None = None):
        self.react_scripts = react_scripts
        self.structured_scripts = structured_scripts
        self.usage_spec = usage or {}
        self.react_calls = 0
        self.structured_calls = 0
        self.structured_messages: list[list[dict]] = []

    async def async_generate(self, *args, result=None, **kwargs):
        self.react_calls += 1
        spec = self.react_scripts[min(self.react_calls - 1, len(self.react_scripts) - 1)]
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield build_message_event(spec.get("content", ""))
        return

    async def generate_structured(self, messages, schema, model_key="fast", max_tokens=None, usage=None):
        self.structured_calls += 1
        self.structured_messages.append(messages)
        spec = self.structured_scripts[
            min(self.structured_calls - 1, len(self.structured_scripts) - 1)
        ]
        if isinstance(spec, Exception):
            raise spec
        if usage is not None and self.usage_spec:
            usage.update(self.usage_spec)
        return spec


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _react_scripts_with_draft(draft: dict | None = None) -> list[dict]:
    """ReAct 脚本：工具收集 → final_answer 提交结构化初稿。"""
    scripts = [
        {
            "finish_reason": "tool_calls",
            "tool_calls": [_tool_call("echo", {"text": "query=alerts"})],
        }
    ]
    if draft is not None:
        scripts.append(
            {
                "finish_reason": "tool_calls",
                "tool_calls": [_tool_call("final_answer", draft)],
            }
        )
    else:
        scripts.append({"finish_reason": "stop", "content": "自由文本回答"})
    return scripts


def _make_strategy(llm, *, error_handlers=None, max_refine_rounds=2) -> ReflectionStrategy:
    tools = ToolService(max_concurrent_tools=10)
    tools.register(_EchoTool())
    return ReflectionStrategy(
        llm=llm,
        tools=tools,
        error_handlers=error_handlers,
    )


async def _run(strategy, max_refine_rounds=2) -> list[str]:
    events = []
    async for ev in strategy.execute(
        "分析良率下降原因",
        [{"role": "user", "content": "分析良率下降原因"}],
        max_iterations=5,
        temperature=0.2,
        max_tokens=1024,
        max_refine_rounds=max_refine_rounds,
    ):
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_reflect_ok_adopts_draft():
    """收集→初稿→自查 ok→采用初稿（degraded=False, refine_rounds=0）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[{"ok": True, "issues": []}],
    )
    strategy = _make_strategy(llm)
    events = await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.draft == DRAFT
    assert strategy.outcome.degraded is False
    assert strategy.outcome.refine_rounds == 0
    assert strategy.outcome.success is True
    assert any("自查通过" in e for e in events)
    assert any('"type": "done"' in e for e in events)


@pytest.mark.asyncio
async def test_reflect_issues_refine_adopts_refined():
    """自查 issues→修正→复查 refined ok→采用 refined（refine_rounds=1，真迭代）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "critical", "dimension": "grounding", "description": "证据不足"}]},
            REFINED,  # 修正
            {"ok": True, "issues": []},  # 复查 refined → ok
        ],
    )
    strategy = _make_strategy(llm)
    events = await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == REFINED
    assert strategy.outcome.refine_rounds == 1
    assert strategy.outcome.degraded is False
    assert strategy.outcome.critique["ok"] is True  # 最后一次自查（修正后复查）通过
    assert any("修正第 1 轮" in e for e in events)


@pytest.mark.asyncio
async def test_reflect_grounding_injected_to_critique():
    """Grounding 断言：自查入参含证据链记录（剔除 final_answer）+ 初稿 JSON。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[{"ok": True, "issues": []}],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert len(llm.structured_messages) == 1
    content = llm.structured_messages[0][0]["content"]
    assert "echo" in content  # 证据链含工具记录
    assert "final_answer" not in content  # final_answer 被剔除
    assert json.dumps(DRAFT, ensure_ascii=False)[:50] in content  # 初稿注入


@pytest.mark.asyncio
async def test_reflect_critique_none_degrades_to_draft():
    """自查返回 None→采用初稿（degraded=True）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[None],
    )
    strategy = _make_strategy(llm)
    events = await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.degraded is True
    assert "自查失败" in strategy.outcome.error
    assert any("降级" in e for e in events)


@pytest.mark.asyncio
async def test_reflect_critique_refusal_degrades_to_draft():
    """自查抛 StructuredRefusalError→走 CRITIQUE_FAILED 分发→采用初稿（degraded=True）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[StructuredRefusalError("拒答")],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.degraded is True


@pytest.mark.asyncio
async def test_reflect_refine_none_degrades_to_draft():
    """自查 issues + 修正返回 None→采用初稿（degraded=True, refine_rounds=0）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺信号"}]},
            None,
        ],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.refine_rounds == 0
    assert strategy.outcome.degraded is True


@pytest.mark.asyncio
async def test_reflect_max_refine_rounds_one_no_refine():
    """max_refine_rounds=1→自查 issues 但 0 次修正，直接采用初稿。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[{"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺信号"}]}],
    )
    strategy = _make_strategy(llm)
    await _run(strategy, max_refine_rounds=1)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.refine_rounds == 0
    assert llm.structured_calls == 1  # 只有自查，无修正


@pytest.mark.asyncio
async def test_reflect_react_no_structured_degrades():
    """ReAct 以 stop 结束（无 final_answer）→content 保留、structured=None、degraded=True。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(None),
        structured_scripts=[],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured is None
    assert strategy.outcome.content == "自由文本回答"
    assert strategy.outcome.degraded is True
    assert "未产出结构化初稿" in strategy.outcome.error


@pytest.mark.asyncio
async def test_reflect_react_error_passthrough():
    """ReAct 失败→error 透传、证据链保留、degraded=True。"""
    llm = _ReflectionLLM(
        react_scripts=[
            {"finish_reason": "", "content": "", "error": "401 认证失败"},
        ],
        structured_scripts=[],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.error == "401 认证失败"
    assert strategy.outcome.success is False
    assert strategy.outcome.degraded is True


@pytest.mark.asyncio
async def test_reflect_critique_failed_raise_propagates():
    """CRITIQUE_FAILED handler 决策 RAISE→抛 AgentRunError。"""
    registry = ErrorHandlerRegistry()

    async def raise_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.RAISE

    registry.register(AgentErrorKind.CRITIQUE_FAILED, raise_handler)
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[StructuredRefusalError("拒答")],
    )
    strategy = _make_strategy(llm, error_handlers=registry)

    with pytest.raises(AgentRunError) as exc_info:
        await _run(strategy)
    assert exc_info.value.kind == AgentErrorKind.CRITIQUE_FAILED


@pytest.mark.asyncio
async def test_reflect_critique_failed_stop_fails_whole():
    """CRITIQUE_FAILED handler 决策 STOP→整个 Reflection 失败（outcome.success=False）。"""
    registry = ErrorHandlerRegistry()

    async def stop_handler(ctx: AgentErrorContext) -> AgentErrorAction:
        return AgentErrorAction.STOP

    registry.register(AgentErrorKind.CRITIQUE_FAILED, stop_handler)
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[StructuredRefusalError("拒答")],
    )
    strategy = _make_strategy(llm, error_handlers=registry)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.success is False
    assert "STOP" in strategy.outcome.error


@pytest.mark.asyncio
async def test_reflect_draft_refined_valid_schema():
    """draft 与 refined 均通过 REFLECTION_SCHEMA 校验（程序校验形状契约）。"""
    import jsonschema

    jsonschema.validate(DRAFT, REFLECTION_SCHEMA)
    jsonschema.validate(REFINED, REFLECTION_SCHEMA)
    assert CRITIQUE_SCHEMA["required"] == ["ok"]


def test_critique_prompt_exhausts_dimensions():
    """Scope 盲区教训：自查清单穷举全部 9 个 dimension 关键字（防清单外漏报）。"""
    dimensions = [
        "grounding",
        "consistency_with_data",
        "fabrication",
        "confidence_calibration",
        "attribution",
        "evidence_gap",
        "completeness",
        "internal_consistency",
        "next_steps_actionable",
    ]
    for dim in dimensions:
        assert dim in CRITIQUE_PROMPT, f"自查清单遗漏维度: {dim}"
    assert "唯一自查范围" in CRITIQUE_PROMPT


def test_reflection_schema_is_strict():
    """schema 契约：required 字段与 additionalProperties:false 约束。"""
    assert REFLECTION_SCHEMA["required"] == ["summary", "conclusions", "next_steps"]
    assert REFLECTION_SCHEMA["additionalProperties"] is False
    assert CRITIQUE_SCHEMA["additionalProperties"] is False


class _FakeCostLimiter:
    def check(self, usage):
        return False, 0.0


class _FakeContextBudget:
    def trim_messages(self, messages, *, max_rounds=None, max_tokens=None):
        return None


@pytest.mark.asyncio
async def test_reflect_guardrails_passthrough_to_react():
    """护栏透传：cost_limiter / context_budget 注入内部 _react；cancel_event 透传。"""
    cl = _FakeCostLimiter()
    cb = _FakeContextBudget()
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[{"ok": True, "issues": []}],
    )
    tools = ToolService(max_concurrent_tools=10)
    tools.register(_EchoTool())
    strategy = ReflectionStrategy(llm=llm, tools=tools, context_budget=cb, cost_limiter=cl)

    cancel_event = asyncio.Event()
    async for _ in strategy.execute(
        "x", [{"role": "user", "content": "x"}],
        max_iterations=5, temperature=0.2, max_tokens=1024,
        cancel_event=cancel_event,
    ):
        pass

    assert strategy._react._cost_limiter is cl
    assert strategy._react._context_budget is cb


REFINED2 = {
    "summary": "良率下降归因于设备 A 告警（最终版）",
    "conclusions": [
        {
            "claim": "设备 A 告警与良率下降时间吻合，且历史类似 excursion 佐证",
            "supporting_evidence": ["echo: query=alerts", "echo: query=yield", "echo: query=history"],
            "confidence": 0.9,
        }
    ],
    "next_steps": ["查询设备 A 详细告警记录"],
    "explicit_abstention": [],
}


@pytest.mark.asyncio
async def test_reflect_recritique_issues_refines_again():
    """真迭代：修正后复查发现新 issues → 再修正 → 复查 ok → 采用（refine_rounds=2）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "critical", "dimension": "grounding", "description": "证据不足"}]},
            REFINED,  # 修正 1
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺历史佐证"}]},  # 复查 refined → 新 issues
            REFINED2,  # 修正 2
            {"ok": True, "issues": []},  # 复查 refined2 → ok
        ],
    )
    strategy = _make_strategy(llm)
    events = await _run(strategy, max_refine_rounds=3)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == REFINED2
    assert strategy.outcome.refine_rounds == 2
    assert strategy.outcome.degraded is False
    assert strategy.outcome.critique["ok"] is True
    assert any("修正第 2 轮" in e for e in events)


@pytest.mark.asyncio
async def test_reflect_reaches_limit_adopts_last():
    """真迭代达上限：修正后复查仍有 issues 且达 max_refine_rounds → 采用最后修正稿（degraded=True）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "critical", "dimension": "grounding", "description": "证据不足"}]},
            REFINED,  # 修正 1
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "仍缺信号"}]},  # 复查 refined → 仍有 issues → 达上限
        ],
    )
    strategy = _make_strategy(llm)
    events = await _run(strategy)  # max_refine_rounds=2

    assert strategy.outcome is not None
    assert strategy.outcome.structured == REFINED  # 采用最后修正稿
    assert strategy.outcome.refine_rounds == 1
    assert strategy.outcome.degraded is True
    assert "达到修正上限" in strategy.outcome.error
    assert any("达到修正上限" in e for e in events)


class _FakeCostLimiterThreshold:
    """按累计 total_tokens 超阈值返回超限（react 阶段 usage 为空不超，reflection 累计后超）。"""

    def __init__(self, threshold: float):
        self.threshold = threshold

    def check(self, usage):
        total = usage.get("total_tokens", 0)
        return total > self.threshold, float(total)


@pytest.mark.asyncio
async def test_reflect_usage_accumulated():
    """critique/refine 的 token 用量累计到 outcome（total_tokens/usage 合并 react + 结构化）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺信号"}]},
            REFINED,
            {"ok": True, "issues": []},
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    # 自查 + 修正 + 复查 = 3 次结构化调用，每次 total 30 → 累计 90（react 阶段无 usage）
    assert strategy.outcome is not None
    assert strategy.outcome.total_tokens == 90
    assert strategy.outcome.usage["total_tokens"] == 90
    assert strategy.outcome.usage["prompt_tokens"] == 30
    assert strategy.outcome.usage["completion_tokens"] == 60


@pytest.mark.asyncio
async def test_reflect_done_event_tokens_match_outcome():
    """done 事件 total_tokens 与 outcome 一致（全阶段累计，P2 口径修复）。

    修复前：done 事件只用 react 阶段 total_tokens，漏计 critique/refine 用量——
    SSE 事件与 outcome 两个事实源漂移，成本审计失真。
    """
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺信号"}]},
            REFINED,
            {"ok": True, "issues": []},
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )
    strategy = _make_strategy(llm)
    events = await _run(strategy)

    assert strategy.outcome is not None
    done = [e for e in events if '"type": "done"' in e]
    assert done
    # 抑制 ReAct 中间 done 后事件流仅 1 个 done（Reflection 收尾产出），
    # total_tokens 须与 outcome 一致（90）——修复前为 react 阶段值 0
    assert f'"total_tokens": {strategy.outcome.total_tokens}' in done[-1]


@pytest.mark.asyncio
async def test_reflect_suppresses_react_done_event():
    """抑制 ReAct 中间 done：事件流只保留 Reflection 收尾的 1 个 done（噪音治理）。

    修复前：Reflection 透传 ReAct 收集阶段的 done + 自己收尾的 done = 2 个，
    且两处 done 的 total_tokens 口径不同（react 仅收集阶段）——消费方困惑。
    """
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[{"ok": True, "issues": []}],
    )
    strategy = _make_strategy(llm)
    events = await _run(strategy)

    done = [e for e in events if '"type": "done"' in e]
    assert len(done) == 1  # 仅 Reflection 收尾 done（修复前 = 2）
    assert f'"total_tokens": {strategy.outcome.total_tokens}' in done[0]


@pytest.mark.asyncio
async def test_reflect_cost_limit_stops():
    """成本护栏：自查/修正累计超限 → 停机降级采用当前稿（error 记录成本超限）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺信号"}]},
            REFINED,
            {"ok": True, "issues": []},
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )
    tools = ToolService(max_concurrent_tools=10)
    tools.register(_EchoTool())
    strategy = ReflectionStrategy(
        llm=llm, tools=tools, cost_limiter=_FakeCostLimiterThreshold(20)
    )
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.degraded is True
    assert "成本超限" in strategy.outcome.error
    assert strategy.outcome.structured == REFINED  # 采用最近修正稿


# ── P3：反思循环终止护栏（用户取消 / 总时长超限 → 停机降级采用最近稿）──


def test_should_abort_cancel_event():
    """用户取消 → 终止（原因含「用户取消」）。"""
    strategy = _make_strategy(_ReflectionLLM([], []))
    cancel = asyncio.Event()
    cancel.set()
    aborted, reason = strategy._should_abort(cancel, 0.0, None)
    assert aborted is True
    assert "用户取消" in reason


def test_should_abort_timeout():
    """总时长超限（start 在过去）→ 终止（原因含「执行超时」）。"""
    import time as _time

    strategy = _make_strategy(_ReflectionLLM([], []))
    aborted, reason = strategy._should_abort(
        None, start_time=_time.monotonic() - 100, max_execution_time=5
    )
    assert aborted is True
    assert "执行超时" in reason


def test_should_abort_ok():
    """无取消 + 未超时 → 不终止。"""
    strategy = _make_strategy(_ReflectionLLM([], []))
    aborted, reason = strategy._should_abort(None, 0.0, None)
    assert aborted is False
    assert reason == ""


@pytest.mark.asyncio
async def test_reflect_cancel_event_stops_degrades_to_draft():
    """循环中取消（自查后置位）→ 停机降级采用最近稿（degraded=True，error 标注用户取消）。

    注：cancel 检查在迭代顶部——自查返回 issues 后进入修正（本迭代内不再检查），
    修正成功 current=refined，下一迭代顶部检测到取消 → 采用 refined（最近稿）。
    """
    cancel_event = asyncio.Event()

    class _CancelOnCritiqueLLM(_ReflectionLLM):
        async def generate_structured(self, messages, schema, model_key="fast", max_tokens=None, usage=None):
            cancel_event.set()  # 自查调用后置位取消（模拟运行中用户取消）
            return await super().generate_structured(messages, schema, model_key, max_tokens, usage)

    llm = _CancelOnCritiqueLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "minor", "dimension": "completeness", "description": "缺信号"}]},
            REFINED,
        ],
    )
    strategy = _make_strategy(llm)
    events = []
    async for ev in strategy.execute(
        "分析良率下降原因",
        [{"role": "user", "content": "分析良率下降原因"}],
        max_iterations=5,
        temperature=0.2,
        max_tokens=1024,
        cancel_event=cancel_event,
    ):
        events.append(ev)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == REFINED  # 采用最近修正稿
    assert strategy.outcome.degraded is True
    assert strategy.outcome.refine_rounds == 1
    assert "用户取消" in strategy.outcome.error
    assert any("降级" in e for e in events)


# ── REASON-010：自查/修正异常面收口（不可恢复 AppError 降级；编程错误冒泡）──
# 注：StructuredTruncationError 不在缺口内——StructuredOutput.extract 对截断短路
# 返回 None（不向上抛），Reflection 走既有「critique is None → 降级」路径（REASON-010）。


@pytest.mark.asyncio
async def test_reflect_critique_nonretryable_degrades_to_draft():
    """自查抛 NonRetryableError（熔断等不可恢复 AppError）→ 降级采用初稿（degraded=True）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[NonRetryableError("熔断开启")],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.degraded is True
    assert "自查失败" in strategy.outcome.error


@pytest.mark.asyncio
async def test_reflect_refine_nonretryable_degrades_to_draft():
    """修正抛 NonRetryableError（熔断等不可恢复 AppError）→ 降级采用初稿（degraded=True）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "critical", "dimension": "grounding", "description": "证据不足"}]},
            NonRetryableError("上游服务熔断"),
        ],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.refine_rounds == 0
    assert strategy.outcome.degraded is True
    assert "修正失败" in strategy.outcome.error


@pytest.mark.asyncio
async def test_reflect_critique_llm_api_error_degrades_to_draft():
    """自查抛 LLMAPIError(401)（集成层归一后的 openai 认证错误）→ 降级采用初稿（REASON-010 闭环）。

    修复前：openai 401 未归一为 AppError，`except AppError` 接不住 → 冒泡整次失败。
    修复后：LLMAPIError 属 AppError 树 → CRITIQUE_FAILED 分发降级。
    """
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[LLMAPIError("下游 401 认证失败", status_code=401)],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.degraded is True
    assert "自查失败" in strategy.outcome.error


@pytest.mark.asyncio
async def test_reflect_refine_llm_api_error_degrades_to_draft():
    """修正抛 LLMAPIError(403)（归一后的 openai 权限错误）→ 降级采用初稿（degraded=True）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[
            {"ok": False, "issues": [{"severity": "critical", "dimension": "grounding", "description": "证据不足"}]},
            LLMAPIError("下游 403 权限拒绝", status_code=403),
        ],
    )
    strategy = _make_strategy(llm)
    await _run(strategy)

    assert strategy.outcome is not None
    assert strategy.outcome.structured == DRAFT
    assert strategy.outcome.refine_rounds == 0
    assert strategy.outcome.degraded is True
    assert "修正失败" in strategy.outcome.error


@pytest.mark.asyncio
async def test_reflect_critique_bug_propagates():
    """自查抛非 AppError 编程错误（TypeError）→ 不吞、向上冒泡（fail fast，防止掩盖 bug）。"""
    llm = _ReflectionLLM(
        react_scripts=_react_scripts_with_draft(DRAFT),
        structured_scripts=[TypeError("模拟编程错误")],
    )
    strategy = _make_strategy(llm)

    with pytest.raises(TypeError):
        await _run(strategy)
