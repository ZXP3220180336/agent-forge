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
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.domain.prompts.manager import PromptManager
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import build_done_event, build_info_event
from app.shared.exceptions import StructuredRefusalError, StructuredToolCallError

from .react import ReActOutcome, ReActStrategy

# 证据链记录中的 final_answer 条目（终止工具，非真实证据）在序列化时剔除
_FINAL_ANSWER_TOOL = "final_answer"


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
    "required": ["summary", "conclusions", "next_steps"],
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
    total_tokens: int = (
        0  # react 累计 token（critique/refine 无 usage 回传，见 benchmark ⚠️）
    )
    usage: dict | None = None
    error: str | None = None
    success: bool = False


class ReflectionStrategy:
    """
    Reflection 循环策略（生成 → 自查 → 修正）。

    构造注入端口依赖 + 可选 schema 覆盖；execute() 完成后通过 outcome 读取结果。
    内部复用 ReActStrategy 做「收集 + 结构化初稿」。
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
        self._error_handlers = error_handlers or ErrorHandlerRegistry()
        self._output_schema = output_schema or REFLECTION_SCHEMA
        self._critique_schema = critique_schema or CRITIQUE_SCHEMA
        # 增强项「异模型 critic」入口：critic/refine 默认走 fast，可构造注入覆盖
        self._critique_model_key = critique_model_key
        # 结果载体，execute() 结束后读取
        self.outcome: ReflectionOutcome | None = None

    async def execute(
        self,
        user_input: str,
        messages: list[dict[str, str]],
        *,
        max_iterations: int,
        temperature: float,
        max_tokens: int,
        max_execution_time: float | None = None,
        max_context_rounds: int | None = None,
        max_context_tokens: int | None = None,
        max_empty_retries: int = 2,
        max_llm_fail_retries: int = 2,
        max_same_action_turns: int = 3,
        tool_timeout: int | None = None,
        tool_max_retries: int | None = None,
        max_refine_rounds: int = 2,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[str]:
        """
        Reflection 主流程：收集+初稿 → 自查 → 修正（三阶段显式分离）。

        迭代上限语义：max_refine_rounds = 报告生成尝试总次数（初稿 1 + 至多 N-1 次修正）。
        降级路由（best-effort，不抛错）：react 失败 / 无 structured / 自查失败 / 修正失败
        → 采用最近稿（degraded=True）。

        Yields:
            SSE 事件字符串（react 事件 + 阶段 info + done）
        """
        # ── 阶段一：收集 + 初稿（复用 ReAct，工具证据链 + final_answer 结构化）──
        async for event in self._react.execute(
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
            max_same_action_turns=max_same_action_turns,
            tool_timeout=tool_timeout,
            tool_max_retries=tool_max_retries,
            output_schema=self._output_schema,
            cancel_event=cancel_event,
        ):
            yield event

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
            self.outcome = self._finalize(
                react_outcome, error=react_outcome.error, success=False, degraded=True
            )
            yield build_done_event(
                iterations=react_outcome.iterations,
                total_tokens=react_outcome.total_tokens,
            )
            return

        # 模型未调用 final_answer（stop 自由文本结束）→ 降级
        if react_outcome.structured is None:
            self.outcome = self._finalize(
                react_outcome,
                success=bool(react_outcome.content.strip()),
                degraded=True,
                error="模型未产出结构化初稿（未调用 final_answer），降级为自由文本",
            )
            yield build_done_event(
                iterations=react_outcome.iterations,
                total_tokens=react_outcome.total_tokens,
            )
            return

        draft = react_outcome.structured
        evidence = react_outcome.tool_calls

        # ── 阶段二：自查（Critic 上下文隔离 + Grounding）──
        yield build_info_event("生成初稿完成，进入自查")
        critique: dict | None = None
        try:
            critique = await self._generate_critique(evidence, draft)
        except (StructuredRefusalError, StructuredToolCallError) as e:
            action = await self._dispatch_critique_failed(
                f"自查生成失败: {e}", react_outcome.iterations
            )
            if action == AgentErrorAction.RAISE:
                raise AgentRunError(
                    AgentErrorKind.CRITIQUE_FAILED,
                    f"自查生成失败: {e}",
                    react_outcome.iterations,
                )
            if action == AgentErrorAction.STOP:
                self.outcome = self._finalize(
                    react_outcome,
                    draft=draft,
                    error=f"自查失败（STOP）: {e}",
                    success=False,
                    degraded=True,
                )
                yield build_done_event(
                    iterations=react_outcome.iterations,
                    total_tokens=react_outcome.total_tokens,
                )
                return
            # CONTINUE → 降级采用 draft

        if critique is None:
            self.outcome = self._finalize(
                react_outcome,
                draft=draft,
                structured=draft,
                critique=None,
                success=bool(draft),
                degraded=True,
                error="自查失败，采用初稿（降级）",
            )
            yield build_info_event("自查失败，采用初稿（降级）")
            yield build_done_event(
                iterations=react_outcome.iterations,
                total_tokens=react_outcome.total_tokens,
            )
            return

        if critique.get("ok"):
            self.outcome = self._finalize(
                react_outcome,
                draft=draft,
                structured=draft,
                critique=critique,
                refine_rounds=0,
                success=True,
            )
            yield build_info_event("自查通过，采用初稿")
            yield build_done_event(
                iterations=react_outcome.iterations,
                total_tokens=react_outcome.total_tokens,
            )
            return

        issues = critique.get("issues") or []
        yield build_info_event(f"自查发现 {len(issues)} 个问题，进入修正")

        # ── 阶段三：修正（issues 回喂 + 完整上下文重写，ground-truth 兜底）──
        best = draft
        for refine_round in range(1, max_refine_rounds):
            yield build_info_event(f"修正第 {refine_round} 轮")
            refined: dict | None = None
            try:
                refined = await self._generate_refine(evidence, best, issues)
            except (StructuredRefusalError, StructuredToolCallError) as e:
                action = await self._dispatch_critique_failed(
                    f"修正失败: {e}", react_outcome.iterations
                )
                if action == AgentErrorAction.RAISE:
                    raise AgentRunError(
                        AgentErrorKind.CRITIQUE_FAILED,
                        f"修正失败: {e}",
                        react_outcome.iterations,
                    )
                if action == AgentErrorAction.STOP:
                    self.outcome = self._finalize(
                        react_outcome,
                        draft=draft,
                        structured=best,
                        critique=critique,
                        refine_rounds=refine_round - 1,
                        error=f"修正失败（STOP）: {e}",
                        success=bool(best),
                        degraded=True,
                    )
                    yield build_done_event(
                        iterations=react_outcome.iterations,
                        total_tokens=react_outcome.total_tokens,
                    )
                    return
                # CONTINUE → 降级采用 best

            if refined is None:
                self.outcome = self._finalize(
                    react_outcome,
                    draft=draft,
                    structured=best,
                    critique=critique,
                    refine_rounds=refine_round - 1,
                    success=bool(best),
                    degraded=True,
                    error="修正失败，采用最近初稿（降级）",
                )
                yield build_info_event("修正失败，采用最近初稿（降级）")
                yield build_done_event(
                    iterations=react_outcome.iterations,
                    total_tokens=react_outcome.total_tokens,
                )
                return
            best = refined

        # 达修正上限 → 采用最近成功稿（best-effort）
        self.outcome = self._finalize(
            react_outcome,
            draft=draft,
            structured=best,
            critique=critique,
            refine_rounds=max_refine_rounds - 1,
            success=bool(best),
        )
        yield build_info_event(f"达到修正上限({max_refine_rounds})，采用最近初稿")
        yield build_done_event(
            iterations=react_outcome.iterations,
            total_tokens=react_outcome.total_tokens,
        )

    # ── 内部辅助 ──

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
    ) -> ReflectionOutcome:
        """从 ReAct outcome 组装 ReflectionOutcome（共享字段透传 + 分支字段覆盖）。"""
        return ReflectionOutcome(
            content=react_outcome.content,
            reasoning=react_outcome.reasoning,
            tool_calls=react_outcome.tool_calls,
            iterations=react_outcome.iterations,
            total_tokens=react_outcome.total_tokens,
            usage=react_outcome.usage,
            structured=structured,
            draft=draft,
            critique=critique,
            refine_rounds=refine_rounds,
            degraded=degraded,
            error=error,
            success=success,
        )

    async def _generate_critique(
        self,
        evidence: list[dict[str, Any]],
        draft: dict[str, Any],
    ) -> dict | None:
        """自查：对照证据链审查初稿，产出结构化自查报告（CRITIQUE_SCHEMA）。"""
        messages = [
            {
                "role": "user",
                "content": PromptManager.build_reflection_critique_prompt(
                    evidence, draft
                ),
            }
        ]
        return await self._llm.generate_structured(
            messages,
            self._critique_schema,
            model_key=self._critique_model_key,
        )

    async def _generate_refine(
        self,
        evidence: list[dict[str, Any]],
        draft: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> dict | None:
        """修正：基于证据链 + 审查意见完整重写（REFLECTION_SCHEMA）。"""
        messages = [
            {
                "role": "user",
                "content": PromptManager.build_reflection_refine_prompt(
                    evidence, draft, issues
                ),
            }
        ]
        return await self._llm.generate_structured(
            messages,
            self._output_schema,
            model_key=self._critique_model_key,
        )

    async def _dispatch_critique_failed(
        self,
        message: str,
        iteration: int,
    ) -> AgentErrorAction:
        """自查/修正失败的错误分发（CRITIQUE_FAILED）。

        CONTINUE = 降级采用最近稿（默认）；STOP = 整个 Reflection 失败；RAISE = 上抛。
        """
        return await self._error_handlers.dispatch(
            AgentErrorKind.CRITIQUE_FAILED,
            AgentErrorContext(
                kind=AgentErrorKind.CRITIQUE_FAILED,
                message=message,
                iteration=iteration,
            ),
        )
