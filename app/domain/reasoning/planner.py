# ============================================
# domain/reasoning/planner.py - Planner 推理策略实现
# ============================================
"""
Planner 推理策略（PlannerStrategy）
==================================

Plan-then-Execute 单 Agent 编排：规划（生成依赖步骤）→ 执行（逐"步"小跑）→ 汇总（结构化报告）。

工业级设计要点（planner_benchmark 对标）：
- Plan-and-Execute：规划器产出目标式依赖步骤，执行器逐"步"——每步复用
  ReActStrategy.execute（被当前步骤约束的 agent 循环，护栏全套免费）
- 结构化规划优先：计划经 JSON Schema（PLAN_SCHEMA/REPLAN_SCHEMA）产出，弃文本解析
- introspection 工具目录：规划器知道有哪些工具（文本目录，可指名）但不调用
  ——generate_structured 恒不传 tools → 无真 tool_calls；步骤 description 写工具
  动作是正常计划语义（非噪音）。工具调用权只在每步执行器（ReAct）
- Replan 核心循环（取舍）：仅步骤失败触发，限 max_replan_rounds 次；不做每步 replan
- 串行单 Agent：depends_on 是顺序纪律断言（列表序即合法拓扑序），并行 DAG 调度留
  Phase C Orchestrator
- 失败降级（best-effort）：规划失败 → 全量 ReAct 兜底；步骤失败 replan 耗尽 →
  部分汇总；汇总 None → 纯文本拼装。均 degraded=True 不抛错

依赖方向：本模块只依赖 ports + shared + prompts，不 import agent/；
被 agent/planner.py（PlannerAgent）编排调用。
"""

import asyncio
import copy
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Any

from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_execution import ToolFact
from app.domain.ports.tool_gateway import ToolGateway
from app.domain.prompts.manager import PromptManager
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorKind,
    ErrorHandlerRegistry,
)
from app.shared.events import (
    AgentEventType,
    build_done_event,
    build_info_event,
)
from app.shared.exceptions import AppError, ContextWindowExceededError

from ._common import (
    GuardResult,
    dispatch_error,
    evaluate_guard,
    merge_usage,
    reject_concurrent_runs,
)
from ._planner_steps import build_plan_payload, build_step_record, normalize_steps
from .execution import (
    ContextWindowLimits,
    ExecutionLimits,
    ModelOptions,
    ReasoningRunScope,
    RecoveryBudget,
    ToolExecutionOptions,
)
from .react import ReActOutcome, ReActStrategy

# ─────────────────────────────────────────────────────────────
# Schema 契约（模块常量 + 构造注入覆盖，不进 AgentContext）
# ─────────────────────────────────────────────────────────────
PLAN_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": (
                "目标式步骤：写明需获取的信息 / 建议使用的工具（若有，来自工具目录）与"
                "查询目标 / 要产出的工件与形态 / 可判定完成的标准——单次工具执行循环可独立"
                "完成，不得依赖本步未完成的其他步骤，也不得重复已完成工作"
            ),
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "description": (
                "本步执行前必须已完成的前置步骤编号（指本列表中更靠前的步骤或已完成步骤）；"
                "无前置依赖填 []（必填，防模型跳过排序思考）"
            ),
        },
    },
    "required": ["description", "depends_on"],
    "additionalProperties": False,
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "重述任务目标（一句话，含可判定的成功标准）",
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": PLAN_STEP_SCHEMA,
            "description": "执行步骤（列表顺序 = 推荐执行顺序；每步自包含；通常 2~5 步）",
        },
    },
    "required": ["goal", "steps"],
    "additionalProperties": False,
}

REPLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "为何重规划（引用失败步骤 + 失败原因，一句话）",
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": PLAN_STEP_SCHEMA,
            "description": (
                "替换当前计划尚未执行剩余部分的新步骤列表（含对失败步骤的修订/拆分/替代）；"
                "基于已完成步骤继续，绝不重复已完成工作"
            ),
        },
    },
    "required": ["reason", "steps"],
    "additionalProperties": False,
}

RESULT_SCHEMA: dict[str, Any] = {
    # 证据链报告形状（与 REFLECTION_SCHEMA 同构但独立发布：结构化最终输出走产品主链路
    # 「证据链报告」方向；不 import reflection 避免模块耦合，允许未来分叉）
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "结论摘要（一句话）"},
        "conclusions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string", "description": "结论陈述"},
                    "supporting_evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "证据出处（工具名 + 查询参数，可审计）",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "置信度分级（高置信度需强/多来源支撑）",
                    },
                },
                "required": ["claim", "supporting_evidence", "confidence"],
                "additionalProperties": False,
            },
            "description": "结论列表（每条可回溯步骤证据）",
        },
        "next_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "建议下一步 / 补充数据（可执行的具体查询或行动）",
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "明确假设",
        },
        "explicit_abstention": {
            "type": "array",
            "items": {"type": "string"},
            "description": "证据不足显式放弃（不硬编结论，空数组=无放弃）",
        },
    },
    "required": ["summary", "conclusions", "next_steps", "explicit_abstention"],
    "additionalProperties": False,
}


@dataclass
class PlannerOutcome:
    """Planner 策略执行的最终结果载体（供桥接方组装 AgentResult）。"""

    content: str = ""  # 自由文本（ReAct 兜底 / 纯文本汇总路径 / summary 摘要）
    reasoning: str = ""  # 兜底 ReAct 末轮 reasoning
    structured: dict | None = None  # 最终 RESULT_SCHEMA 结构化（证据链报告）
    plan: dict | None = (
        None  # 生效计划 {goal, steps:[{id, description}]}；兜底路径 None
    )
    steps_executed: list[dict] = field(
        default_factory=list
    )  # 步骤审计记录（含失败步，见 execute）
    replan_rounds: int = 0  # 实际重规划次数
    degraded: bool = False  # True=经降级路径（规划失败→ReAct / 步骤失败→部分汇总 / 汇总 None→纯文本）
    tool_calls: list[dict[str, Any]] = field(
        default_factory=list
    )  # 聚合证据链（各步 tool_calls 平铺）
    iterations: int = 0  # 各步 react.iterations 之和
    total_tokens: int = 0  # react 各步 + 全阶段 structured 累计
    usage: dict | None = None
    error: str | None = None
    success: bool = False


class PlannerStrategy:
    """
    Planner 循环策略（规划 → 执行 → 汇总）。

    构造注入端口依赖 + 可选 schema 覆盖；execute() 完成后通过 outcome 读取结果。
    内部复用 ReActStrategy 做每步执行（被步骤约束的 agent 循环）与全量 ReAct 兜底。

    约束：实例单次执行——outcome / 各累计态在每次 execute 覆盖，不并发复用同一实例。
    """

    def __init__(
        self,
        llm: LLMGateway,
        tools: ToolGateway,
        context_budget: ContextBudgetPort | None = None,
        error_handlers: ErrorHandlerRegistry | None = None,
        cost_limiter: CostLimiterPort | None = None,
        plan_schema: dict[str, Any] | None = None,
        replan_schema: dict[str, Any] | None = None,
        result_schema: dict[str, Any] | None = None,
        plan_model_key: str = "fast",
        summarize_model_key: str = "fast",
    ) -> None:
        # 每步执行复用 ReActStrategy（护栏透传内部 _react）
        self._react = ReActStrategy(
            llm, tools, context_budget, error_handlers, cost_limiter
        )
        self._llm = llm
        self._tools = tools
        # 只复用统一 token 计量；Planner 的字段优先级由 prompts 包内策略决定。
        self._context_budget = context_budget
        self._error_handlers = error_handlers or ErrorHandlerRegistry()
        self._cost_limiter = cost_limiter
        self._plan_schema = plan_schema or PLAN_SCHEMA
        self._replan_schema = replan_schema or REPLAN_SCHEMA
        self._result_schema = result_schema or RESULT_SCHEMA
        # 规划/重规划/汇总结构化调用默认走 fast（可构造注入覆盖）
        self._plan_model_key = plan_model_key
        self._summarize_model_key = summarize_model_key
        # 结果载体，execute() 结束后读取
        self.outcome: PlannerOutcome | None = None
        self._tool_facts: list[ToolFact] = []

    @property
    def tool_facts(self) -> tuple[ToolFact, ...]:
        """返回本 run 所有步骤及中断子跑已经接管的事实快照。"""
        return tuple(copy.deepcopy(self._tool_facts))

    # ==================================================================
    # execute 主流程
    # ==================================================================

    @reject_concurrent_runs
    async def execute(
        self,
        user_input: str,
        messages: list[dict[str, str]],
        *,
        run: ReasoningRunScope,
        model: ModelOptions,
        limits: ExecutionLimits,
        context_window: ContextWindowLimits,
        recovery: RecoveryBudget,
        tool_execution: ToolExecutionOptions,
        stream_mode: bool = True,
    ) -> AsyncGenerator[str]:
        """
        Planner 主流程：规划 → 执行 → 汇总（三阶段显式分离）。

        语义：
        - ``limits.max_iterations`` 是每步 ReAct 小跑迭代上限；总预算由
          ``limits.max_execution_time``（全局墙钟，每步转剩余预算）与 cost_limiter 兜底
        - ``recovery.max_replan_rounds`` 是步骤失败触发的重规划次数上限；Planner
          要求该值非 None，0 表示不允许重规划
        - 运行身份、模型、上下文、恢复和工具限制分别由对应不可变值对象承载；
          每步 ReAct 复用同一运行作用域，并只替换剩余墙钟
        - depends_on = 顺序纪律断言（串行单 Agent，列表序即合法拓扑序）；执行前守卫
          depends_on ⊆ 已完成（normalize 阶段已丢弃未知/自引/前瞻引用）
        - 降级路由（best-effort，不抛错）：规划失败 → 全量 ReAct 兜底；步骤失败
          replan 耗尽 → 部分汇总；汇总 None → 纯文本拼装

        Yields:
            SSE 事件字符串（阶段 info + 步骤 tool 事件透传 + 收尾 done）
        """
        if recovery.max_replan_rounds is None:
            raise ValueError("PlannerStrategy 要求设置 max_replan_rounds")

        # 每次 execute 独立：重置全部累计态
        self.outcome = None
        self._tool_facts = []
        self._structured_usage: dict = {}  # 结构化调用（plan/replan/summarize）用量
        self._react_total_usage: dict = {}  # 各步/兜底 react 用量累计
        self._step_llm_iterations: int = 0  # 各步 react.iterations 之和
        self._last_structured_error: str | None = None  # 最近一次结构化调用失败原因
        self._replan_used: int = 0  # 实际 replan 次数
        start_time = time.monotonic()
        # E：结构化调用（规划/汇总）的绝对截止——与各阶段 guard 同一时间预算（monotonic
        # 绝对时刻，不逐级重计），随 generate_structured 下沉到降级链每笔子调用前。
        deadline = (
            start_time + limits.max_execution_time
            if limits.max_execution_time is not None
            else None
        )
        executed: list[dict] = []  # 步骤审计记录（含失败步）
        completed: set[int] = set()  # 成功步骤 id（depends_on 守卫）
        tool_catalog = self._tool_catalog()

        def _current_guard(
            context_error: ContextWindowExceededError | None = None,
        ) -> GuardResult | None:
            """按 Planner 全阶段累计 usage 判定是否允许继续副作用。"""
            return evaluate_guard(
                cancel_event=run.cancel_event,
                deadline=deadline,
                cost_limiter=self._cost_limiter,
                running_usage=merge_usage(
                    self._react_total_usage, self._structured_usage
                ),
                context_error=context_error,
            )

        # 每步/兜底的 ReAct 子跑：护栏参数透传本 execute，仅输入（任务文本 + 消息）不同。
        # 收敛为局部闭包，避免两处 19 行参数重复（改护栏只需改一处）；事件透传并抑制
        # 中间 done（REASON-011：收尾 _finalize 统一产 done）。
        async def _run_react(
            text: str, sub_messages: list[dict]
        ) -> AsyncGenerator[str]:
            """跑一次 ReAct 子跑并透传其事件（抑制中间 done）；剩余预算每次调用现算
            （全局墙钟差额，下界 0.05s）。

            cost 护栏跨阶段贯通：透传当前累计（已完成步骤 react + 结构化用量）作
            baseline_usage——子跑内每轮即按累计成本检查（对齐 cost-limit ADR「调用后
            立即检查」），子跑中途累计越界在越界轮停，报告口径仍局部（_absorb_react
            各归并一次，防双计）。"""
            child = self._react.execute(
                text,
                sub_messages,
                run=run,
                model=model,
                limits=replace(
                    limits,
                    max_execution_time=(
                        max(
                            0.05,
                            limits.max_execution_time - (time.monotonic() - start_time),
                        )
                        if limits.max_execution_time is not None
                        else None
                    ),
                ),
                context_window=context_window,
                recovery=recovery,
                tool_execution=tool_execution,
                stream_mode=stream_mode,
                baseline_usage=merge_usage(
                    self._react_total_usage, self._structured_usage
                ),
            )
            try:
                async with aclosing(child):
                    async for event in child:
                        if f'"type": "{AgentEventType.DONE.value}"' in event:
                            continue
                        yield event
            finally:
                # 下一步骤会重置子策略；父 run 必须先接管，包括异常/关闭出口。
                self._tool_facts.extend(self._react.tool_facts)

        # ── 阶段 A：规划（进度事件前置，发起付费调用前统一终止/成本护栏）──
        yield build_info_event("进入规划阶段")

        guard = _current_guard()
        if guard is not None:
            msg = f"{guard.message}，未开始规划"
            for e in self._finalize(
                plan=None,
                steps_executed=[],
                success=False,
                degraded=True,
                error=msg,
                info=msg,
            ):
                yield e
            return

        plan, plan_action, plan_usage, plan_context_error = await self._plan(
            user_input,
            tool_catalog,
            max_context_tokens=context_window.max_tokens,
            cancel_event=run.cancel_event,
            deadline=deadline,
        )

        if plan_usage:
            self._structured_usage = merge_usage(self._structured_usage, plan_usage)

        guard = _current_guard(plan_context_error)
        if guard is not None:
            # plan 非 None（规划已产出但未开工）：给出契约形状快照，与 plan_result 同源——
            # PlannerOutcome.plan 恒为 {goal, steps:[{id, description}]}，消费方口径唯一。
            suffix = (
                "规划失败后中止，未降级兜底"
                if plan is None
                else "规划后中止，未开始执行"
            )
            msg = f"{guard.message}，{suffix}"
            for e in self._finalize(
                plan=(
                    build_plan_payload(
                        plan["goal"],
                        normalize_steps(plan["steps"], completed=completed),
                    )
                    if plan
                    else None
                ),
                steps_executed=[],
                success=False,
                degraded=True,
                error=msg,
                info=msg,
            ):
                yield e
            return

        if plan is None:
            # 规划失败（None=结构化降级耗尽；AppError 已分发 PLAN_FAILED）→ 降级全量 ReAct 兜底。
            # 兜底也是付费调用：发起前终止/成本护栏——取消/超成本不再兜底，直接失败收尾。
            suffix = "（STOP）" if plan_action == AgentErrorAction.STOP else ""
            if plan_action == AgentErrorAction.STOP:
                msg = f"规划失败{suffix}，终止执行"
                for e in self._finalize(
                    plan=None,
                    steps_executed=[],
                    success=False,
                    degraded=True,
                    error=self._last_structured_error or msg,
                    info=msg,
                ):
                    yield e
                return
            yield build_info_event(f"规划失败{suffix}，降级为直接 ReAct")

            async with aclosing(_run_react(user_input, list(messages))) as child:
                async for event in child:
                    yield event

            rb = self._react.outcome
            self._absorb_react(rb)
            for e in self._finalize(
                plan=None,
                steps_executed=[],
                success=(
                    plan_action != AgentErrorAction.STOP
                    and rb is not None
                    and rb.success
                ),
                degraded=True,
                error=(
                    rb.error
                    if rb and rb.error
                    else self._last_structured_error
                    or f"规划失败{suffix}，已降级为直接 ReAct"
                ),
                content=rb.content if rb else "",
                reasoning=rb.reasoning if rb else "",
                info=f"规划失败{suffix}，已降级为直接 ReAct",
            ):
                yield e
            return

        goal = plan["goal"]
        pending = normalize_steps(plan["steps"], completed=completed)
        if not pending:
            for e in self._finalize(
                plan=None,
                steps_executed=[],
                success=False,
                degraded=True,
                error="计划步骤为空（normalize 后无可执行步骤），降级",
                info="计划步骤为空，降级",
            ):
                yield e
            return
        plan_result = build_plan_payload(goal, pending)

        # ── 阶段 B：执行（串行；ready 守卫 depends_on ⊆ completed）──
        while pending:
            guard = _current_guard()
            if guard is not None:
                msg = f"{guard.message}，采用已完成步骤（部分进度）"
                for e in self._finalize(
                    structured=None,
                    plan=plan_result,
                    steps_executed=executed,
                    success=any(r["success"] for r in executed),
                    degraded=True,
                    error=msg,
                    info=msg,
                ):
                    yield e
                return

            step = pending.pop(0)
            yield build_info_event(f"执行步骤 {step['id']}: {step['description'][:60]}")
            sub_messages = self._step_messages(goal, step, executed, messages)
            async with aclosing(_run_react(step["description"], sub_messages)) as child:
                async for event in child:
                    yield event

            # 子跑返回后先吸收其真实成果、usage 与迭代，再按统一优先级决定是否继续。
            sub = self._react.outcome
            self._absorb_react(sub)
            # 记录后再检查 Guard：取消 / 超时 / 成本同时到达时保留当前步骤事实。
            step_record = build_step_record(step, sub)
            executed.append(step_record)
            ok = step_record["success"]

            guard = _current_guard()
            if guard is not None:
                for e in self._finalize_partial(
                    plan_result,
                    executed,
                    degraded=True,
                    error=f"{guard.message}，采用已完成步骤（部分进度）",
                    info=f"{guard.message}，采用已完成步骤（部分进度）",
                ):
                    yield e
                return

            if ok:
                completed.add(step["id"])
                continue

            # ── 步骤失败 → replan（限 max_replan_rounds）──
            yield build_info_event(f"步骤 {step['id']} 失败，进入重规划")

            failed_step = executed[-1]
            pending, replan_guard, replan_action = await self._replan_loop(
                goal,
                tool_catalog,
                executed,
                failed_step,
                deadline,
                run.cancel_event,
                recovery.max_replan_rounds,
                context_window.max_tokens,
            )

            if replan_guard is not None:
                for e in self._finalize_partial(
                    plan_result,
                    executed,
                    degraded=True,
                    error=f"{replan_guard.message}，采用已完成步骤（部分进度）",
                ):
                    yield e
                return

            if replan_action == AgentErrorAction.STOP:
                for e in self._finalize_partial(
                    plan_result,
                    executed,
                    degraded=True,
                    error=self._last_structured_error or "重规划失败（STOP）",
                    info="重规划失败（STOP），采用已完成步骤（部分进度）",
                ):
                    yield e
                return

            if pending is None:
                # replan 耗尽/失败：有成功步尝试结构化汇总产部分报告；否则纯失败
                if any(r["success"] for r in executed):
                    # 发起汇总前终止/成本护栏：取消/成本超限 → 直接降级采用已完成步骤
                    guard = _current_guard()
                    if guard is not None:
                        for e in self._finalize_partial(
                            plan_result,
                            executed,
                            degraded=True,
                            error=f"{guard.message}，采用已完成步骤（部分进度）",
                        ):
                            yield e
                        return

                    sub_result, _, sub_usage, sub_context_error = await self._summarize(
                        goal,
                        executed,
                        max_context_tokens=context_window.max_tokens,
                        cancel_event=run.cancel_event,
                        deadline=deadline,
                    )

                    if sub_usage:
                        self._structured_usage = merge_usage(
                            self._structured_usage, sub_usage
                        )

                    guard = _current_guard(sub_context_error)
                    if guard is not None:
                        for e in self._finalize_guarded_summary(
                            guard, sub_result, plan_result, executed
                        ):
                            yield e
                        return

                    if sub_result is not None:
                        for e in self._finalize(
                            structured=sub_result,
                            plan=plan_result,
                            steps_executed=executed,
                            success=True,
                            degraded=True,
                            content=str(sub_result.get("summary", ""))[:200],
                            info="重规划未恢复，基于已完成步骤产出部分报告",
                        ):
                            yield e
                        return

                for e in self._finalize_partial(
                    plan_result,
                    executed,
                    degraded=True,
                    error="重规划未恢复，采用已完成步骤（部分进度）",
                    info="重规划未恢复，采用已完成步骤（部分进度）",
                ):
                    yield e
                return
            # 重规划得到新尾 → 回到 while 顶部执行（继续任务未中断）

        # ── 阶段 C：汇总（进度事件前置，发起付费调用前统一终止/成本护栏）──
        yield build_info_event("执行完成，进入汇总")

        guard = _current_guard()
        if guard is not None:
            for e in self._finalize_partial(
                plan_result,
                executed,
                degraded=True,
                error=f"{guard.message}，采用已完成步骤（部分进度）",
            ):
                yield e
            return

        result, action, result_usage, result_context_error = await self._summarize(
            goal,
            executed,
            max_context_tokens=context_window.max_tokens,
            cancel_event=run.cancel_event,
            deadline=deadline,
        )

        if result_usage:
            self._structured_usage = merge_usage(self._structured_usage, result_usage)

        guard = _current_guard(result_context_error)
        if guard is not None:
            for e in self._finalize_guarded_summary(
                guard, result, plan_result, executed
            ):
                yield e
            return

        if result is None:
            # 汇总失败（None / AppError 分发）→ 纯文本拼装降级
            suffix = "（STOP）" if action == AgentErrorAction.STOP else ""
            plain = self._plain_summary(executed)
            for e in self._finalize(
                structured=None,
                plan=plan_result,
                steps_executed=executed,
                success=action != AgentErrorAction.STOP and bool(plain),
                degraded=True,
                error=f"汇总未产出结构化结果{suffix}，降级为纯文本拼装",
                content=plain,
                info=f"汇总未产出结构化结果{suffix}，降级为纯文本拼装",
            ):
                yield e
            return

        for e in self._finalize(
            structured=result,
            plan=plan_result,
            steps_executed=executed,
            success=True,
            content=str(result.get("summary", ""))[:200],
        ):
            yield e

    # ==================================================================
    # 重规划循环（独立方法保持主循环可读）
    # ==================================================================

    async def _replan_loop(
        self,
        goal: str,
        tool_catalog: str,
        executed: list[dict],
        failed_step: dict,
        deadline: float | None,
        cancel_event: asyncio.Event | None,
        max_replan_rounds: int,
        max_context_tokens: int | None = None,
    ) -> tuple[
        list[dict] | None,
        GuardResult | None,
        AgentErrorAction | None,
    ]:
        """步骤失败后重规划循环：至多 max_replan_rounds 次，每次产出新尾替换未执行部分。

        Returns:
            ``(新尾步骤, Guard, 分发动作)``。新尾为 None 且 Guard/动作均为空表示
            replan 耗尽或普通失败；Guard 或 STOP 动作非空时，调用方必须立即收尾。
        """
        while self._replan_used < max_replan_rounds:
            # 每轮 replan 也是付费结构化调用：发起前终止/成本护栏（对齐每步 react）——
            # 超限 → 返回 None 由调用方降级，不再发起付费 replan（不消耗 replan 预算）
            guard = evaluate_guard(
                cancel_event=cancel_event,
                deadline=deadline,
                cost_limiter=self._cost_limiter,
                running_usage=merge_usage(
                    self._react_total_usage, self._structured_usage
                ),
            )
            if guard is not None:
                return None, guard, None

            self._replan_used += 1
            new_tail, action, usage, context_error = await self._replan(
                goal,
                tool_catalog,
                executed,
                failed_step,
                max_context_tokens=max_context_tokens,
                cancel_event=cancel_event,
                deadline=deadline,
            )

            if usage:
                self._structured_usage = merge_usage(self._structured_usage, usage)

            guard = evaluate_guard(
                cancel_event=cancel_event,
                deadline=deadline,
                cost_limiter=self._cost_limiter,
                running_usage=merge_usage(
                    self._react_total_usage, self._structured_usage
                ),
                context_error=context_error,
            )
            if guard is not None:
                return None, guard, action

            if action == AgentErrorAction.STOP:
                return None, None, action

            if not new_tail:
                return None, None, action  # replan 失败 / 空计划 → 无补救

            # 新尾 id 续接已执行步最大 id 之后（单调计数，防依赖错位）
            completed_ids = {r["id"] for r in executed if r["success"]}
            start_no = max([r["id"] for r in executed], default=0) + 1
            pending = normalize_steps(
                new_tail, completed=completed_ids, start_no=start_no
            )
            if pending:
                return pending, None, None
            # replan 出的步骤全部无法执行（依赖不可满足）→ 继续尝试下次 replan
        return None, None, None

    # ==================================================================
    # 结构化调用（plan / replan / summarize）+ 失败分发
    # ==================================================================

    async def _plan(
        self,
        user_input: str,
        tool_catalog: str,
        *,
        max_context_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> tuple[
        dict | None,
        AgentErrorAction | None,
        dict | None,
        ContextWindowExceededError | None,
    ]:
        """规划：generate_structured 产出 PLAN_SCHEMA。

        返回 ``(计划, 分发动作, 用量, 上下文错误)``。上下文超限由调用方按
        Guard 终止；其余失败走 PLAN_FAILED 分发（默认 CONTINUE=降级 ReAct 兜底；
        STOP=硬失败；RAISE 抛 AgentRunError）。非 AppError 编程错误冒泡 fail fast。
        """
        count_tokens = (
            self._context_budget.count_tokens
            if self._context_budget is not None
            else None
        )
        prompt = PromptManager.build_planning_prompt(
            user_input,
            tool_catalog,
            max_tokens=max_context_tokens,
            count_tokens=count_tokens,
        )
        context_error = self._prompt_context_error(
            prompt,
            model_key=self._plan_model_key,
            max_context_tokens=max_context_tokens,
            count_tokens=count_tokens,
        )
        if context_error is not None:
            self._last_structured_error = str(context_error)
            return None, None, None, context_error
        messages = [
            {
                "role": "user",
                "content": prompt,
            }
        ]
        usage: dict = {}
        try:
            result = await self._llm.generate_structured(
                messages,
                self._plan_schema,
                model_key=self._plan_model_key,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            if result is None:
                self._last_structured_error = "规划（结构化降级耗尽）"
            return result, None, usage or None, None
        except ContextWindowExceededError as e:
            self._last_structured_error = str(e)
            return None, None, usage or None, e
        except AppError as e:
            self._last_structured_error = str(e)
            action = await dispatch_error(
                self._error_handlers,
                AgentErrorKind.PLAN_FAILED,
                f"规划失败: {e}",
                iteration=0,
            )
            # 保留链内已成功调用的 usage（拒答/不可恢复上抛前已发生的真实消耗）
            return None, action, usage or None, None

    async def _replan(
        self,
        goal: str,
        tool_catalog: str,
        executed: list[dict],
        failed_step: dict,
        *,
        max_context_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> tuple[
        list[dict] | None,
        AgentErrorAction | None,
        dict | None,
        ContextWindowExceededError | None,
    ]:
        """重规划：基于已完成步骤 + 失败步骤产出新尾（REPLAN_SCHEMA.steps）。

        返回 ``(新尾步骤列表, 分发动作, 用量, 上下文错误)``。上下文超限由调用方
        按 Guard 终止；普通失败返回 ``(None, action, usage, None)``。
        """
        count_tokens = (
            self._context_budget.count_tokens
            if self._context_budget is not None
            else None
        )
        prompt = PromptManager.build_planning_replan_prompt(
            goal,
            tool_catalog,
            executed,
            failed_step,
            failed_step.get("error") or "",
            max_tokens=max_context_tokens,
            count_tokens=count_tokens,
        )
        context_error = self._prompt_context_error(
            prompt,
            model_key=self._plan_model_key,
            max_context_tokens=max_context_tokens,
            count_tokens=count_tokens,
        )
        if context_error is not None:
            self._last_structured_error = str(context_error)
            return None, None, None, context_error
        messages = [
            {
                "role": "user",
                "content": prompt,
            }
        ]
        usage: dict = {}
        try:
            result = await self._llm.generate_structured(
                messages,
                self._replan_schema,
                model_key=self._plan_model_key,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            if result is None:
                return None, None, usage or None, None
            return result.get("steps"), None, usage or None, None
        except ContextWindowExceededError as e:
            self._last_structured_error = str(e)
            return None, None, usage or None, e
        except AppError as e:
            self._last_structured_error = str(e)
            action = await dispatch_error(
                self._error_handlers,
                AgentErrorKind.PLAN_FAILED,
                f"重规划失败: {e}",
                iteration=0,
            )
            # 保留链内已成功调用的 usage（拒答/不可恢复上抛前已发生的真实消耗）
            return None, action, usage or None, None

    async def _summarize(
        self,
        goal: str,
        executed: list[dict],
        *,
        max_context_tokens: int | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> tuple[
        dict | None,
        AgentErrorAction | None,
        dict | None,
        ContextWindowExceededError | None,
    ]:
        """汇总：基于各步骤结果产出 RESULT_SCHEMA（证据链报告）。

        返回 ``(报告, 分发动作, 用量, 上下文错误)``。上下文超限由调用方按
        Guard 终止；普通失败返回 ``(None, action, usage, None)``。
        """
        count_tokens = (
            self._context_budget.count_tokens
            if self._context_budget is not None
            else None
        )
        prompt = PromptManager.build_planning_summarize_prompt(
            goal,
            executed,
            max_tokens=max_context_tokens,
            count_tokens=count_tokens,
        )
        context_error = self._prompt_context_error(
            prompt,
            model_key=self._summarize_model_key,
            max_context_tokens=max_context_tokens,
            count_tokens=count_tokens,
        )
        if context_error is not None:
            return None, None, None, context_error
        messages = [
            {
                "role": "user",
                "content": prompt,
            }
        ]
        usage: dict = {}
        try:
            result = await self._llm.generate_structured(
                messages,
                self._result_schema,
                model_key=self._summarize_model_key,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            return result, None, usage or None, None
        except ContextWindowExceededError as e:
            return None, None, usage or None, e
        except AppError as e:
            action = await dispatch_error(
                self._error_handlers,
                AgentErrorKind.PLAN_FAILED,
                f"汇总失败: {e}",
                iteration=0,
            )
            # 保留链内已成功调用的 usage（拒答/不可恢复上抛前已发生的真实消耗）
            return None, action, usage or None, None

    # ==================================================================
    # 内部辅助
    # ==================================================================

    @staticmethod
    def _prompt_context_error(
        prompt: str,
        *,
        model_key: str,
        max_context_tokens: int | None,
        count_tokens: Callable[[str], int] | None,
    ) -> ContextWindowExceededError | None:
        """在 SDK 调用前拒绝连最小 Planner 语义骨架也装不下的提示词。"""
        if count_tokens is None or max_context_tokens is None:
            return None
        input_tokens = count_tokens(prompt)
        if input_tokens <= max_context_tokens:
            return None
        return ContextWindowExceededError(
            model_key=model_key,
            input_tokens=input_tokens,
            input_budget=max_context_tokens,
            max_tokens=0,
        )

    def _absorb_react(self, outcome: ReActOutcome | None) -> None:
        """一次 react 子跑归并进累计态（usage / iterations）。"""
        if outcome is None:
            return
        self._react_total_usage = merge_usage(
            getattr(self, "_react_total_usage", None), outcome.usage
        )
        self._step_llm_iterations += outcome.iterations or 0

    def _tool_catalog(self) -> str:
        """工具目录 introspection 文本（名 + 描述；仅供规划指名，不入 tools 参数）。"""
        if self._tools is None:
            return "（无可用工具）"
        lines: list[str] = []
        for tool in self._tools.get_openai_tools():
            name = tool.get("function", {}).get("name", "")
            desc = tool.get("function", {}).get("description", "")
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines) or "（无可用工具）"

    def _step_messages(
        self,
        goal: str,
        step: dict,
        executed: list[dict],
        messages: list[dict],
    ) -> list[dict]:
        """每步隔离上下文：system 前缀 + 已完成步骤摘要 + 当前步骤指令。

        不累积原始工具 transcript（防跨步 token 线性膨胀 + 步骤独立）。
        步骤自包含前提：需要上一步数值就在摘要里带，未覆盖则应合并成一步。
        """
        system = [m for m in messages if m.get("role") == "system"]
        # 已完成步骤摘要：每步只带结果摘要（[:500]，不带原始 tool transcript），
        # 整体 [:4000] 限长——防跨步上下文膨胀 + 保步骤独立
        lines: list[str] = []
        for rec in executed:
            status = "成功" if rec["success"] else f"失败({rec['error']})"
            summary = (rec["summary"] or rec["content"] or "")[:500]
            lines.append(f"[步骤 {rec['id']}]{status}: {summary}")

        body = (
            f"任务目标：{goal}\n\n"
            f"已完成步骤结果：\n{'\n'.join(lines)[:4000]}\n\n"
            f"当前步骤（只执行这一步，完成后用文字给出该步产出与结论）：\n"
            f"{step['description']}\n"
        )
        return [*system, {"role": "user", "content": body}]

    def _finalize_partial(
        self,
        plan_result: dict | None,
        executed: list[dict],
        degraded: bool = True,
        error: str = "",
        info: str = "",
    ) -> list[str]:
        """部分汇总降级收尾：组装纯文本拼装（_plain_summary）的最终 outcome。

        供调用方已判定「不发起新付费调用」的收尾（取消 / 成本超限 / 无成功步等）：
        纯文本拼装保留已完成步骤可见产出；有成功步算部分成功，否则失败。
        需先尝试结构化汇总的路径由调用方 await _summarize 后自行决定。
        """
        plain = self._plain_summary(executed)
        return self._finalize(
            structured=None,
            plan=plan_result,
            steps_executed=executed,
            success=bool(plain) and any(r["success"] for r in executed),
            degraded=degraded,
            error=error,
            content=plain,
            info=info,
        )

    def _finalize_guarded_summary(
        self,
        guard: GuardResult,
        result: dict | None,
        plan_result: dict | None,
        executed: list[dict],
    ) -> list[str]:
        """汇总调用后护栏命中：保留已返回汇总，否则采用步骤纯文本。"""
        if result is None:
            return self._finalize_partial(
                plan_result,
                executed,
                degraded=True,
                error=f"{guard.message}，采用已完成步骤（部分进度）",
            )
        message = f"{guard.message}，保留已生成汇总"
        return self._finalize(
            structured=result,
            plan=plan_result,
            steps_executed=executed,
            success=True,
            degraded=True,
            error=message,
            content=str(result.get("summary", ""))[:200],
            info=message,
        )

    def _plain_summary(self, executed: list[dict]) -> str:
        """各步结果纯文本拼装（汇总结构化失败降级用）。"""
        parts: list[str] = []
        for rec in executed:
            status = "成功" if rec["success"] else f"失败({rec['error']})"
            parts.append(
                f"步骤 {rec['id']}（{status}）：{rec['summary'] or rec['content'] or ''}"
            )
        return "\n".join(parts)

    # ==================================================================
    # _finalize 收尾（同步 list[str]）
    # ==================================================================

    def _finalize(
        self,
        *,
        structured: dict | None = None,
        plan: dict | None = None,
        steps_executed: list[dict] | None = None,
        replan_rounds: int | None = None,
        degraded: bool = False,
        error: str | None = None,
        success: bool = False,
        info: str = "",
        content: str = "",
        reasoning: str = "",
    ) -> list[str]:
        """组装 PlannerOutcome 并返回收尾事件（可选 info + 必选 done）。

        非 async：无 await，直接组事件列表。total_tokens = react 各步累计 +
        _structured_usage 合并（事件流与结果对象口径对齐，REASON-011 模式）。
        """
        executed = steps_executed or []
        self.outcome = PlannerOutcome(
            content=content,
            reasoning=reasoning,
            structured=structured,
            plan=plan,
            steps_executed=executed,
            replan_rounds=self._replan_used if replan_rounds is None else replan_rounds,
            degraded=degraded,
            error=error,
            success=success,
            tool_calls=[tc for rec in executed for tc in rec.get("tool_calls", [])],
            iterations=self._step_llm_iterations,
            total_tokens=sum(r.get("total_tokens", 0) for r in executed)
            + self._structured_usage.get("total_tokens", 0),
            usage=merge_usage(
                getattr(self, "_react_total_usage", None), self._structured_usage
            )
            or None,
        )
        events: list[str] = []
        if info:
            events.append(build_info_event(info))
        events.append(
            build_done_event(
                iterations=self._step_llm_iterations,
                total_tokens=self.outcome.total_tokens,
            )
        )
        return events
