# ============================================
# domain/reasoning/reflection.py - Reflection 推理策略实现
# ============================================
"""
Reflection 推理策略（ReflectionStrategy）
========================================

生成（Generate）→ 自查（Critique）→ 修正（Refine）三阶段，模型自我评估输出质量并改进。

工业级设计要点（reflection_benchmark 对标）：
- 三阶段显式分离：生成与评判是不同认知任务，独立方法，禁止混在单一 prompt
- Grounding（证据锚定）为核心：critic 必须对照工具记录（证据链）自查，
  杜绝纯内在自查（Huang et al. 反证：内在自查会让模型把对的改错）
- 生成阶段复用 ReActStrategy.execute(output_schema=...)：工具收集 + final_answer
  结构化初稿一次完成（Reflexion「生成即工具增强」），证据链在 outcome.tool_calls
- 失败降级（best-effort）：自查/修正失败采用最近稿（degraded=True），不抛错

依赖方向：本模块只依赖 ports + shared + prompts（指令层），不 import agent/；
被 agent/reflection.py（ReflectionAgent）编排调用。
"""

import asyncio
import copy
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
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
from .react import ReActOutcome, ReActStrategy

# ─────────────────────────────────────────────────────────────
# Schema 契约（模块常量 + 构造注入覆盖，不进 AgentContext）
# ─────────────────────────────────────────────────────────────
REFLECTION_SCHEMA: dict[str, Any] = {
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
            "description": "结论列表",
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
            "description": "证据不足显式放弃（不硬编结论）",
        },
    },
    "required": ["summary", "conclusions", "next_steps", "explicit_abstention"],
    "additionalProperties": False,
}

CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "description": "自查是否通过"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "minor"],
                        "description": "严重级别（critical=必须修正 / minor=建议改进）",
                    },
                    "dimension": {
                        "type": "string",
                        "enum": [
                            "grounding",
                            "consistency_with_data",
                            "fabrication",
                            "confidence_calibration",
                            "attribution",
                            "evidence_gap",
                            "completeness",
                            "internal_consistency",
                            "next_steps_actionable",
                        ],
                        "description": (
                            "自查维度（穷举清单，Scope 盲区教训）："
                            "grounding=结论可回溯工具记录 / "
                            "consistency_with_data=数值与工具结果一致 / "
                            "fabrication=无编造 / "
                            "attribution=结论与证据配对正确 / "
                            "confidence_calibration=置信度与证据强度匹配 / "
                            "evidence_gap=证据不足应显式放弃 / "
                            "completeness=关键信号覆盖 / "
                            "internal_consistency=各部分自洽 / "
                            "next_steps_actionable=下一步可执行"
                        ),
                    },
                    "claim": {"type": "string", "description": "定位初稿的哪条结论"},
                    "description": {"type": "string", "description": "问题描述"},
                },
                "required": ["severity", "dimension", "description"],
                "additionalProperties": False,
            },
            "description": "自查发现的问题列表",
        },
    },
    "required": ["ok"],
    "additionalProperties": False,
}


@dataclass
class ReflectionOutcome:
    """Reflection 策略执行的最终结果载体（供桥接方组装 AgentResult）。"""

    content: str = ""  # 最终自由文本（react 降级路径时保留）
    reasoning: str = ""  # react 末轮 reasoning_content
    structured: dict | None = None  # 最终结构化（refine 后取 refine 稿；否则 draft）
    draft: dict | None = None  # 初稿（自查/修正的输入）
    critique: dict | None = None  # 自查结果 {ok, issues[]}；None=自查失败
    refine_rounds: int = 0  # 实际修正轮数
    degraded: bool = False  # True=经降级路径（自查/修正失败/react 无 structured）
    tool_calls: list[dict[str, Any]] = field(
        default_factory=list
    )  # 证据链（来自 react outcome）
    iterations: int = 0  # react 循环轮数（证据链收集轮）
    total_tokens: int = 0  # 累计 token用量（包含react阶段 + critique）
    usage: dict | None = None
    error: str | None = None
    success: bool = False


class ReflectionStrategy:
    """
    Reflection 循环策略（生成 → 自查 → 修正）。

    构造注入端口依赖 + 可选 schema 覆盖；execute() 完成后通过 outcome 读取结果。
    内部复用 ReActStrategy 做「收集 + 结构化初稿」。

    约束：实例单次执行——outcome / _structured_usage 在每次 execute 覆盖，
    不并发复用同一实例（每次运行新建，或串行调用后及时读取 outcome）。
    """

    def __init__(
        self,
        llm: LLMGateway,
        tools: ToolGateway,
        context_budget: ContextBudgetPort | None = None,
        error_handlers: ErrorHandlerRegistry | None = None,
        cost_limiter: CostLimiterPort | None = None,
        output_schema: dict[str, Any] | None = None,
        critique_schema: dict[str, Any] | None = None,
        critique_model_key: str = "fast",
    ) -> None:
        # 收集阶段复用 ReActStrategy（护栏透传内部 _react）
        self._react = ReActStrategy(
            llm, tools, context_budget, error_handlers, cost_limiter
        )
        self._llm = llm
        self._context_budget = context_budget
        self._error_handlers = error_handlers or ErrorHandlerRegistry()
        self._cost_limiter = cost_limiter
        self._output_schema = output_schema or REFLECTION_SCHEMA
        self._critique_schema = critique_schema or CRITIQUE_SCHEMA
        # 增强项「异模型 critic」入口：critic/refine 默认走 fast，可构造注入覆盖
        self._critique_model_key = critique_model_key
        # 自查/修正阶段 token 用量累计（execute 开始重置；供成本护栏 + outcome 统计）
        self._structured_usage: dict = {}
        # 结果载体，execute() 结束后读取
        self.outcome: ReflectionOutcome | None = None
        self._tool_facts: list[ToolFact] = []

    @property
    def tool_facts(self) -> tuple[ToolFact, ...]:
        """返回本 run 收集阶段已经接管的工具事实快照。"""
        return tuple(copy.deepcopy(self._tool_facts))

    @reject_concurrent_runs
    async def execute(
        self,
        user_input: str,
        messages: list[dict[str, str]],
        *,
        max_iterations: int,
        temperature: float,
        max_tokens: int,
        run_id: str,
        run_stop: asyncio.Event,
        max_execution_time: float | None = None,
        max_context_rounds: int | None = None,
        max_context_tokens: int | None = None,
        max_empty_retries: int = 2,
        max_llm_fail_retries: int = 2,
        max_tool_protocol_retries: int = 2,
        max_same_action_turns: int = 3,
        tool_timeout: int | None = None,
        tool_max_retries: int | None = None,
        max_refine_rounds: int = 2,
        cancel_event: asyncio.Event | None = None,
        workflow_id: str | None = None,
        parent_cancel_events: tuple[asyncio.Event, ...] = (),
    ) -> AsyncGenerator[str]:
        """
        Reflection 主流程：收集+初稿 → 自查 → 修正（三阶段显式分离）。

        迭代上限语义：max_refine_rounds = 报告生成尝试总次数（初稿 1 + 至多 N-1 次修正）。
        降级路由（best-effort，不抛错）：react 失败 / 无 structured / 自查失败 / 修正失败
        → 采用最近稿（degraded=True）。

        Yields:
            SSE 事件字符串（react 事件 + 阶段 info + done）
        """
        # 每次 execute 独立：重置自查/修正阶段 token 用量累计
        self.outcome = None
        self._tool_facts = []
        self._structured_usage = {}
        # 总时长护栏起点（反思循环顶部检查 elapsed > max_execution_time，P3）
        start_time = time.monotonic()
        # E：结构化调用（自查/修正）的绝对截止——与循环顶部护栏同一时间预算（monotonic
        # 绝对时刻，不逐级重计），随 generate_structured 下沉到降级链每笔子调用前。
        deadline = (
            start_time + max_execution_time if max_execution_time is not None else None
        )

        # ── 阶段一：收集 + 初稿（复用 ReAct，工具证据链 + final_answer 结构化）──
        child = self._react.execute(
            user_input,
            messages,
            max_iterations=max_iterations,
            temperature=temperature,
            max_tokens=max_tokens,
            max_execution_time=max_execution_time,
            max_context_rounds=max_context_rounds,
            max_context_tokens=max_context_tokens,
            max_empty_retries=max_empty_retries,
            max_llm_fail_retries=max_llm_fail_retries,
            max_tool_protocol_retries=max_tool_protocol_retries,
            max_same_action_turns=max_same_action_turns,
            tool_timeout=tool_timeout,
            tool_max_retries=tool_max_retries,
            output_schema=self._output_schema,
            cancel_event=cancel_event,
            run_id=run_id,
            run_stop=run_stop,
            workflow_id=workflow_id,
            parent_cancel_events=parent_cancel_events,
        )
        try:
            async with aclosing(child):
                async for event in child:
                    # 自查/修正阶段统一提交 done，初稿阶段仅透传进度。
                    if f'"type": "{AgentEventType.DONE.value}"' in event:
                        continue
                    yield event
        finally:
            # 即使子跑传播控制异常，事实也先转交父 run，不依赖 outcome 存在。
            self._tool_facts.extend(self._react.tool_facts)

        react_outcome = self._react.outcome
        if react_outcome is None:
            self.outcome = ReflectionOutcome(
                success=False,
                error="ReAct 策略未产出结果",
                degraded=True,
            )
            yield build_done_event(iterations=0, total_tokens=0)
            return

        # react 失败 → 降级（error 透传，证据链保留）
        if react_outcome.error:
            for e in self._finalize(
                react_outcome, error=react_outcome.error, success=False, degraded=True
            ):
                yield e
            return

        # 模型未调用 final_answer（stop 自由文本结束）→ 降级
        if react_outcome.structured is None:
            for e in self._finalize(
                react_outcome,
                success=bool(react_outcome.content.strip()),
                degraded=True,
                error="模型未产出结构化初稿（未调用 final_answer），降级为自由文本",
            ):
                yield e
            return

        draft = react_outcome.structured
        evidence = react_outcome.tool_calls

        # ── 阶段二 + 三：自查 → 修正 → 复查 循环（真迭代，每次修正后重新自查）──
        # 工业标准（Self-Refine / LangGraph）：迭代必须由新反馈驱动——修正 refined 后
        # 重新自查 refined，ok 则采用、新 issues 再修正，直到通过或达 max_refine_rounds。
        # 复用同一批 issues 反复修正 = 反模式（refined 已被处理，旧 issues 无新信息）。
        yield build_info_event("生成初稿完成，进入自查")
        current = draft  # 当前候选稿（初稿 → 各轮修正稿）
        refine_round = 0
        while True:
            # 终止/成本护栏（P3）：发起新付费调用前检查——取消/超时/成本超限
            # → 停机降级采用最近稿（保留进度）
            guard = evaluate_guard(
                cancel_event=cancel_event,
                deadline=deadline,
                cost_limiter=self._cost_limiter,
                running_usage=merge_usage(react_outcome.usage, self._structured_usage),
            )
            if guard is not None:
                for e in self._finalize_guard(
                    guard, react_outcome, draft, current, refine_round
                ):
                    yield e
                return

            # ── 自查当前稿 ──
            (
                critique,
                crit_action,
                crit_usage,
                crit_context_error,
            ) = await self._critique(
                evidence,
                current,
                react_outcome.iterations,
                max_context_tokens=max_context_tokens,
                cancel_event=cancel_event,
                deadline=deadline,
            )

            if crit_usage:
                self._structured_usage = merge_usage(self._structured_usage, crit_usage)

            # generate_structured 会把 cancel/deadline 收敛为 None；上下文准入异常由
            # _critique 显式返回。无可用 critique 时先恢复终止原因，再判普通自查失败。
            if critique is None:
                guard = evaluate_guard(
                    cancel_event=cancel_event,
                    deadline=deadline,
                    cost_limiter=None,
                    running_usage=merge_usage(
                        react_outcome.usage, self._structured_usage
                    ),
                    context_error=crit_context_error,
                )
                if guard is not None:
                    for e in self._finalize_guard(
                        guard, react_outcome, draft, current, refine_round
                    ):
                        yield e
                    return
                # 自查失败 → 降级采用当前稿（best-effort，不抛错）
                suffix = "（STOP）" if crit_action == AgentErrorAction.STOP else ""
                for e in self._finalize(
                    react_outcome,
                    draft=draft,
                    structured=current,
                    critique=None,
                    refine_rounds=refine_round,
                    success=crit_action != AgentErrorAction.STOP and bool(current),
                    degraded=True,
                    error=f"自查失败{suffix}，采用最近稿（降级）",
                    info="自查失败，采用最近稿（降级）",
                ):
                    yield e
                return

            terminal_events = self._finalize_after_critique(
                react_outcome,
                draft,
                current,
                critique,
                refine_round,
                max_refine_rounds,
                deadline,
            )
            if terminal_events is not None:
                for event in terminal_events:
                    yield event
                return

            # 护栏（P3）：执行到此处 = 自查未通过且未达修正上限，即将发起修正这笔付费调用——
            # 本处检查是「修正调用前准入」。两条早退路径（自查通过 / 达到修正上限）之后都不再
            # 付费，故复查不置于其前：已产出的合格稿不因累计预算被改判为降级、自查结论不丢失。
            # 命中（取消 / 超时 / 成本超限）→ 停机降级采用最近稿（保留进度），不发起修正。
            guard = evaluate_guard(
                cancel_event=cancel_event,
                deadline=deadline,
                cost_limiter=self._cost_limiter,
                running_usage=merge_usage(react_outcome.usage, self._structured_usage),
            )
            if guard is not None:
                for e in self._finalize_guard(
                    guard,
                    react_outcome,
                    draft,
                    current,
                    refine_round,
                    critique=critique,
                ):
                    yield e
                return

            # 有 issues 且未达上限 → 修正（issues 回喂 + 完整上下文重写，ground-truth 兜底）
            issues = critique.get("issues") or []
            refine_round += 1
            yield build_info_event(
                f"自查发现 {len(issues)} 个问题，修正第 {refine_round} 轮"
            )
            refined, ref_action, ref_usage, ref_context_error = await self._refine(
                evidence,
                current,
                issues,
                react_outcome.iterations,
                max_context_tokens=max_context_tokens,
                cancel_event=cancel_event,
                deadline=deadline,
            )

            if ref_usage:
                self._structured_usage = merge_usage(self._structured_usage, ref_usage)

            current, completed_rounds = self._adopt_refined_draft(
                current,
                refined,
                refine_round,
            )

            guard = evaluate_guard(
                cancel_event=cancel_event,
                deadline=deadline,
                cost_limiter=self._cost_limiter,
                running_usage=merge_usage(react_outcome.usage, self._structured_usage),
                context_error=ref_context_error,
            )
            if guard is not None:
                for e in self._finalize_guard(
                    guard,
                    react_outcome,
                    draft,
                    current,
                    completed_rounds,
                    critique=critique,
                ):
                    yield e
                return

            if refined is None:
                # 修正失败 → 降级采用当前稿（best-effort）
                suffix = "（STOP）" if ref_action == AgentErrorAction.STOP else ""
                for e in self._finalize(
                    react_outcome,
                    draft=draft,
                    structured=current,
                    critique=critique,
                    refine_rounds=refine_round - 1,
                    success=ref_action != AgentErrorAction.STOP and bool(current),
                    degraded=True,
                    error=f"修正失败{suffix}，采用最近稿（降级）",
                    info="修正失败，采用最近稿（降级）",
                ):
                    yield e
                return
            # 回到循环顶部 → 重新自查修正稿（真迭代的关键：新反馈驱动下一轮）

    # ── 内部辅助 ──

    def _finalize_after_critique(
        self,
        react_outcome: ReActOutcome,
        draft: dict[str, Any],
        current: dict[str, Any],
        critique: dict[str, Any],
        refine_round: int,
        max_refine_rounds: int,
        deadline: float | None,
    ) -> list[str] | None:
        """在 critique 可用后决定提交当前稿，或允许进入下一轮修正。"""
        if not critique.get("ok") and refine_round < max_refine_rounds - 1:
            return None

        # REASON-020：成本和 after-turn cancel 不改判已形成的结论；绝对 deadline
        # 是 strict，迟到稿保留，但不能标记为按时成功。
        deadline_guard = evaluate_guard(
            cancel_event=None,
            deadline=deadline,
            cost_limiter=None,
            running_usage=merge_usage(
                react_outcome.usage,
                self._structured_usage,
            ),
        )
        if deadline_guard is not None:
            return self._finalize_guard(
                deadline_guard,
                react_outcome,
                draft,
                current,
                refine_round,
                critique=critique,
            )

        if critique.get("ok"):
            return self._finalize(
                react_outcome,
                draft=draft,
                structured=current,
                critique=critique,
                refine_rounds=refine_round,
                success=True,
                info="自查通过，采用当前稿",
            )

        return self._finalize(
            react_outcome,
            draft=draft,
            structured=current,
            critique=critique,
            refine_rounds=refine_round,
            success=bool(current),
            degraded=True,
            error=f"达到修正上限({max_refine_rounds})，采用最近稿（未通过自查）",
            info=f"达到修正上限({max_refine_rounds})，采用最近稿",
        )

    @staticmethod
    def _adopt_refined_draft(
        current: dict[str, Any],
        refined: dict[str, Any] | None,
        attempted_rounds: int,
    ) -> tuple[dict[str, Any], int]:
        """只接管完整修正稿，并返回真实完成的修正轮数。"""
        if refined is None:
            return current, attempted_rounds - 1
        return refined, attempted_rounds

    def _finalize_guard(
        self,
        guard: GuardResult,
        react_outcome: ReActOutcome,
        draft: dict[str, Any],
        current: dict[str, Any],
        refine_rounds: int,
        *,
        critique: dict[str, Any] | None = None,
    ) -> list[str]:
        """护栏终止时保留最近完整稿，并由统一判定提供原因。"""
        message = f"{guard.message}，采用最近稿（降级）"
        return self._finalize(
            react_outcome,
            draft=draft,
            structured=current,
            critique=critique,
            refine_rounds=refine_rounds,
            success=bool(current),
            degraded=True,
            error=message,
            info=message,
        )

    async def _critique(
        self,
        evidence: list[dict[str, Any]],
        draft: dict[str, Any],
        iteration: int,
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
        """自查当前稿；失败走 CRITIQUE_FAILED 分发。

        返回 ``(结果, 动作, 用量, 上下文错误)``。上下文超限不进入普通失败分发，
        交由调用方按 Guard 优先级终止；其余 AppError（拒答 / 工具调用 / 截断 /
        认证熔断等）统一分发降级（REASON-010）。RAISE 抛 AgentRunError，非 AppError
        编程错误向上冒泡（fail fast）。
        """
        usage: dict = {}
        try:
            count_tokens = (
                self._context_budget.count_tokens
                if self._context_budget is not None
                else None
            )
            prompt = PromptManager.build_reflection_critique_prompt(
                evidence,
                draft,
                max_tokens=max_context_tokens,
                count_tokens=count_tokens,
            )
            if (
                count_tokens is not None
                and max_context_tokens is not None
                and count_tokens(prompt) > max_context_tokens
            ):
                return (
                    None,
                    None,
                    None,
                    ContextWindowExceededError(
                        model_key=self._critique_model_key,
                        input_tokens=count_tokens(prompt),
                        input_budget=max_context_tokens,
                        max_tokens=0,
                    ),
                )
            messages = [
                {
                    "role": "user",
                    "content": prompt,
                }
            ]
            result = await self._llm.generate_structured(
                messages,
                self._critique_schema,
                model_key=self._critique_model_key,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            return result, None, usage, None
        except ContextWindowExceededError as e:
            return None, None, usage or None, e
        except AppError as e:
            action = await dispatch_error(
                self._error_handlers,
                AgentErrorKind.CRITIQUE_FAILED,
                f"自查生成失败: {e}",
                iteration,
            )
            # 保留链内已成功调用的 usage（refusal/不可恢复上抛前已发生的真实消耗）
            return None, action, usage or None, None

    async def _refine(
        self,
        evidence: list[dict[str, Any]],
        draft: dict[str, Any],
        issues: list[dict[str, Any]],
        iteration: int,
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
        """修正当前稿；失败走 CRITIQUE_FAILED 分发。

        返回 ``(结果, 动作, 用量, 上下文错误)``。上下文超限不进入普通失败分发，
        交由调用方按 Guard 优先级终止；其余 AppError（拒答 / 工具调用 / 截断 /
        认证熔断等）统一分发降级（REASON-010）。RAISE 抛 AgentRunError，非 AppError
        编程错误向上冒泡（fail fast）。
        """
        usage: dict = {}
        try:
            count_tokens = (
                self._context_budget.count_tokens
                if self._context_budget is not None
                else None
            )
            prompt = PromptManager.build_reflection_refine_prompt(
                evidence,
                draft,
                issues,
                max_tokens=max_context_tokens,
                count_tokens=count_tokens,
            )
            if (
                count_tokens is not None
                and max_context_tokens is not None
                and count_tokens(prompt) > max_context_tokens
            ):
                return (
                    None,
                    None,
                    None,
                    ContextWindowExceededError(
                        model_key=self._critique_model_key,
                        input_tokens=count_tokens(prompt),
                        input_budget=max_context_tokens,
                        max_tokens=0,
                    ),
                )
            messages = [
                {
                    "role": "user",
                    "content": prompt,
                }
            ]
            result = await self._llm.generate_structured(
                messages,
                self._output_schema,
                model_key=self._critique_model_key,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            return result, None, usage, None
        except ContextWindowExceededError as e:
            return None, None, usage or None, e
        except AppError as e:
            action = await dispatch_error(
                self._error_handlers,
                AgentErrorKind.CRITIQUE_FAILED,
                f"修正失败: {e}",
                iteration,
            )
            # 保留链内已成功调用的 usage（refusal/不可恢复上抛前已发生的真实消耗）
            return None, action, usage or None, None

    def _finalize(
        self,
        react_outcome: ReActOutcome,
        *,
        structured: dict | None = None,
        draft: dict | None = None,
        critique: dict | None = None,
        refine_rounds: int = 0,
        degraded: bool = False,
        error: str | None = None,
        success: bool = False,
        info: str = "",
    ) -> list[str]:
        """组装 ReflectionOutcome 并返回收尾事件（可选 info + 必选 done）。

        Args:
            react_outcome: ReAct 收集阶段产出（共享字段透传）
            info: 阶段信息事件内容（空串则不产出 info 事件）
            （其余字段为 ReflectionOutcome 分支覆盖字段）

        非 async：无 await，直接组事件列表。每个终止分支只需
        ``for e in self._finalize(...): yield e``。
        """
        self.outcome = ReflectionOutcome(
            content=react_outcome.content,
            reasoning=react_outcome.reasoning,
            tool_calls=react_outcome.tool_calls,
            iterations=react_outcome.iterations,
            total_tokens=react_outcome.total_tokens
            + self._structured_usage.get("total_tokens", 0),
            usage=merge_usage(react_outcome.usage, self._structured_usage) or None,
            structured=structured,
            draft=draft,
            critique=critique,
            refine_rounds=refine_rounds,
            degraded=degraded,
            error=error,
            success=success,
        )
        events: list[str] = []
        if info:
            events.append(build_info_event(info))
        # done 事件 token 口径与 outcome 一致（P2）：react 收集 + 自查/修正全阶段
        # 累计——避免 SSE 事件只报收集阶段、漏计 critique/refine 成本（事件流与
        # 结果对象两个事实源对齐）
        events.append(
            build_done_event(
                iterations=react_outcome.iterations,
                total_tokens=react_outcome.total_tokens
                + self._structured_usage.get("total_tokens", 0),
            )
        )
        return events
