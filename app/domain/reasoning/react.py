# ============================================
# domain/reasoning/react.py - ReAct 推理策略实现
# ============================================
"""
ReAct 推理策略（ReActStrategy）
==============================

推理（Reason）→ 行动（Act）→ 观察（Observe），循环直到完成。

本模块是领域层推理策略库的 ReAct 实现（原子推理策略），被 agent/ 层编排调用：
  ReActAgent._strategy_cycle() → ReActStrategy.execute()
  PlannerAgent 执行阶段 / ReflectionAgent 收集阶段 → ReActStrategy.execute_tool_calls()

依赖方向：本模块只依赖 ports + shared + 标准库，不 import agent/（策略是纯算法，
收标量参数而非 AgentContext），可独立测试、可被任意编排复用。

事件流输出设计（与 executor.py 原 ReActAgent 一致）：
    LLM 原始流 → type=reasoning（逐 token）
               → type=message（逐 token）
    发现 tool_calls → type=tool_call
    执行工具       → type=tool_result
    LLM 下一轮原始流 → type=reasoning / message
    完成           → type=done

每次 execute() 是独立的：结果写入 self.outcome，调用方（ReActAgent）读取后组装 AgentResult。
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from jsonschema import validate

from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway, StreamResult
from app.domain.ports.tool_gateway import ErrorCode, ToolGateway, ToolResult
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorContext,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import (
    build_done_event,
    build_info_event,
    build_tool_call_event,
    build_tool_result_event,
)

# 工具结果回喂截断标记：截断时追加，模型可知结果不完整（而非误以为完整）
_TRUNCATED_MARKER = "\n[结果已截断]"


def _truncate_with_marker(text: str, limit: int) -> str:
    """截断到 limit 字符；截断时追加截断标记（预留标记长度，总长不超 limit）。"""
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER


# 结构化最终答案工具（Final Answer 模式，SMOL / OpenAI 官方）：模型最后调用提交
# schema 约束的结构化结果并终止循环。注入工具（非注册工具，识别在 execute 主循环）。
_FINAL_ANSWER_TOOL = "final_answer"


def _build_final_answer_tool(schema: dict) -> dict:
    """构造 final_answer 工具定义（OpenAI tool schema，参数 = output_schema）。"""
    return {
        "type": "function",
        "function": {
            "name": _FINAL_ANSWER_TOOL,
            "description": (
                "完成任务后调用一次，以符合给定 JSON Schema 的结构化格式提交最终答案。"
                "不得与其他工具混用。"
            ),
            "parameters": schema,
        },
    }


def _extract_final_answer(
    tool_call: dict, schema: dict
) -> tuple[dict | None, str | None]:
    """解析并校验 final_answer 参数；返回 (结构化结果, 错误)。成功时 error 为 None。"""
    try:
        args = json.loads(tool_call["function"]["arguments"])
    except (json.JSONDecodeError, KeyError) as e:
        return None, f"参数 JSON 解析失败: {e}"
    if not isinstance(args, dict):
        return None, "参数应为 JSON 对象"
    try:
        validate(instance=args, schema=schema)
    except Exception as e:  # noqa: BLE001 — jsonschema 校验失败，回喂模型自纠
        return None, f"不符合 schema: {e}"
    return args, None


@dataclass
class ReActOutcome:
    """ReAct 策略执行的最终结果载体（供桥接方组装 AgentResult）。"""

    content: str = ""
    reasoning: str = ""
    structured: dict | None = (
        None  # final_answer 结构化最终答案（output_schema 启用时）
    )
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    total_tokens: int = 0
    usage: dict | None = None
    error: str | None = None
    success: bool = False


class ReActStrategy:
    """
    ReAct 循环策略（纯算法，不持有 Agent 状态）。

    构造注入端口依赖，execute() 完成后通过 outcome 读取结果。
    """

    def __init__(
        self,
        llm: LLMGateway,
        tools: ToolGateway,
        context_budget: ContextBudgetPort | None = None,
        error_handlers: ErrorHandlerRegistry | None = None,
        cost_limiter: CostLimiterPort | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._context_budget = context_budget
        self._error_handlers = error_handlers or ErrorHandlerRegistry()
        self._cost_limiter = cost_limiter
        self._tool_call_records: list[dict[str, Any]] = []
        # 结果载体，execute() 结束后读取
        self.outcome: ReActOutcome | None = None

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
        output_schema: dict | None = None,
    ) -> AsyncGenerator[str]:
        """
        ReAct 主循环。

        循环流程（各分支经错误处理分发，默认行为 = 现有逻辑）：
            0. 上下文预算裁剪（所有继续路径共用，保证每次 LLM 调用前消息有界）
            1. LLM 推理（流式输出 reasoning / message）
            2. 检查 finish_reason
               - "stop"       → 生成最终结果，结束循环
               - "length"     → 生成部分结果，结束循环
               - "tool_calls" → final_answer 检测 → 执行工具，追加结果，继续循环
            3. 达到最大迭代次数 → 错误分发（默认兜底）
            4. 达到 max_execution_time（总时长上限）→ 错误分发（默认超时降级）

        实现：主循环仅保留骨架，各终止/错误分支拆分为职责单一的方法
        （_finalize_* / _handle_*），以 `outcome is not None` 作为终止信号。

        Args:
            user_input: 用户原始输入（保留兼容，循环内部以 messages 为准）
            messages: 可修改的消息列表副本
            max_iterations: 最大迭代轮数
            temperature: LLM 采样温度
            max_tokens: 单轮最大输出 token
            max_execution_time: 整个循环总时长上限（秒），None=不设限（向后兼容）；
                超时对齐 max_iterations 兜底模式降级，error 记录超时原因
            max_context_rounds: 上下文预算——保留最近 N 轮 assistant/tool 配对（None=不裁剪）
            max_context_tokens: 上下文预算——消息总 token 上限（None=不裁剪）
            output_schema: 最终答案结构化 JSON Schema（None=不启用）。启用时注入
                final_answer 工具，模型最后调用提交结构化结果并终止循环

        Yields:
            SSE 事件字符串（reasoning / message / tool_call / tool_result / info / done）
        """
        tool_defs = self._tools.get_openai_tools() if self._tools else None
        # 结构化最终答案：注入 final_answer 工具（模型最后调用提交结构化结果并终止）
        if output_schema is not None:
            tool_defs = [*(tool_defs or []), _build_final_answer_tool(output_schema)]
        has_tools = bool(tool_defs)

        self._tool_call_records = []
        last_result: StreamResult | None = None
        total_usage: dict = {}
        # 记录进入 timeout 的 task：超时降级时判别「真超时」与「生成器被 finalizer
        # 关闭」（慢消费者场景 aclose 由不同 task 驱动，需干净停止不 yield 降级事件）。
        entered_task = asyncio.current_task()

        try:
            async with asyncio.timeout(max_execution_time):
                for iteration in range(1, max_iterations + 1):
                    yield build_info_event(f"第 {iteration} 轮推理")

                    # ----- 0. 上下文预算：模型本次调用前作为 gatekeeper 裁剪 -----
                    # 置于循环顶部（而非工具路径后）：所有继续路径共用——工具回喂、
                    # LLM 失败重试 / final_answer 回喂重试 / 空输出重试，下一次 LLM
                    # 调用前均裁剪，否则非工具路径上下文无限增长、预算失效。
                    if self._context_budget is not None:
                        self._context_budget.trim_messages(
                            messages,
                            max_rounds=max_context_rounds,
                            max_tokens=max_context_tokens,
                        )

                    # ----- 1. LLM 推理 -----
                    stream_result = StreamResult()

                    async for event in self._llm.async_generate(
                        messages=messages,
                        tools=tool_defs,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        result=stream_result,
                    ):
                        yield event

                    last_result = stream_result
                    # 累计 token 用量
                    if stream_result.usage:
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            total_usage[k] = total_usage.get(
                                k, 0
                            ) + stream_result.usage.get(k, 0)

                    # ----- 成本护栏：累计成本超限 → 错误分发（默认 STOP 降级）-----
                    # 置于 error 判断前：成本是全局资源护栏，预算超限时不允许
                    # LLM 失败重试 / 工具执行再产生付费调用或副作用；超限即停机。
                    if self._cost_limiter is not None:
                        exceeded, cost = self._cost_limiter.check(total_usage)
                        if exceeded:
                            async for event in self._finalize_cost_exceeded(
                                stream_result, iteration, total_usage, cost
                            ):
                                yield event
                            return

                    # LLM 调用失败（create 失败 / 流中断放弃 / 用户取消）→ 短路返回失败结果，
                    # 不把「失败」当「空输出」继续空转重试（浪费 LLM 调用 + 错误信息不准确）。
                    # 正常空回（stop + 空 content）error 为 None，仍走下方「空输出重试」逻辑。
                    if stream_result.error:
                        # LLM 调用失败 → 错误分发（默认 STOP 短路；handler 可重试/上抛）
                        async for event in self._finalize_llm_failed(
                            stream_result, iteration, total_usage
                        ):
                            yield event
                        if self.outcome is not None:
                            return
                        continue  # CONTINUE：重试

                    full_reasoning = stream_result.reasoning_content
                    full_content = stream_result.content

                    # ----- 2. 将 LLM 回复追加到消息历史 -----
                    assistant_msg: dict = {
                        "role": "assistant",
                        "content": full_content,
                    }
                    # DeepSeek V4 thinking 模式带 tools 时必须回喂 reasoning_content（否则 400）；
                    # has_reasoning 覆盖空 reasoning 场景（空串也回喂，字段始终存在）
                    if full_reasoning or stream_result.has_reasoning:
                        assistant_msg["reasoning_content"] = full_reasoning
                    # OpenAI 兼容 API 要求：tool 消息必须与前置 assistant 消息的 tool_calls 配对，
                    # 否则下一轮请求 400（"Messages with role 'tool' must be a response to ..."）
                    if stream_result.tool_calls:
                        assistant_msg["tool_calls"] = stream_result.tool_calls
                    messages.append(assistant_msg)

                    # ----- 3. 根据 finish_reason 决定下一步 -----
                    finish_reason = stream_result.finish_reason or ""

                    # ----- （1）调用工具 → 继续调用大模型
                    if finish_reason == "tool_calls" and has_tools:
                        yield build_info_event(
                            f"检测到 {len(stream_result.tool_calls)} 个工具调用"
                        )

                        # Final Answer 工具：结构化最终答案 → 提取终止 / 回喂继续
                        # （注入工具非注册工具，识别在主循环，不进 execute_tool_calls）
                        if output_schema is not None and any(
                            tc["function"]["name"] == _FINAL_ANSWER_TOOL
                            for tc in stream_result.tool_calls
                        ):
                            async for event in self._handle_final_answer(
                                stream_result.tool_calls,
                                messages,
                                iteration,
                                output_schema,
                                total_usage,
                                full_reasoning,
                            ):
                                yield event
                            if self.outcome is not None:
                                return
                            continue  # final_answer CONTINUE：回喂后继续

                        async for event in self._handle_tool_calls(
                            stream_result.tool_calls,
                            messages,
                            iteration,
                            total_usage,
                            full_reasoning,
                        ):
                            yield event
                        if self.outcome is not None:
                            return
                        continue

                    # ----- （2）stop / length / 有内容 → 正常结束
                    if finish_reason in ("stop", "length") or full_content.strip():
                        async for event in self._finalize_stop(
                            full_reasoning, full_content, iteration, total_usage
                        ):
                            yield event
                        return

                    # ----- （3）空输出 → 错误分发（默认 CONTINUE 重试；handler 可终止/上抛）
                    async for event in self._handle_empty_output(
                        full_reasoning, iteration, total_usage
                    ):
                        yield event
                    if self.outcome is not None:
                        return
                    # CONTINUE（默认）：重试（_handle_empty_output 已 yield 重试信息）

                # ----- 达到最大迭代次数 → 错误分发（默认 STOP 兜底；handler 可上抛） -----
                async for event in self._finalize_max_turns(
                    last_result, max_iterations, total_usage
                ):
                    yield event

        except TimeoutError:
            # 关闭判别：生成器正被 finalizer/aclose 关闭（不同 task 驱动）或外部取消
            # → 干净停止，不 yield 降级事件（避免 RuntimeError: async generator ignored GeneratorExit）
            cur = asyncio.current_task()
            if cur is None or cur is not entered_task or cur.cancelling() > 0:
                return

            # 真超时 → 错误分发 + 降级
            async for event in self._finalize_timeout(
                last_result, iteration, total_usage, max_execution_time
            ):
                yield event

            return

    # ==================================================================
    # execute 的拆分方法（职责单一，行为与原内联分支一致）
    # 终止信号：设置 self.outcome = 终止；不设置 = 继续循环（主循环据此 return/continue）
    # ==================================================================

    def _build_outcome(
        self,
        *,
        success: bool,
        content: str,
        reasoning: str,
        iteration: int,
        total_tokens: int,
        usage: dict | None,
        error: str | None = None,
        structured: dict | None = None,
        tool_calls: list[dict] | None = None,
    ) -> ReActOutcome:
        """统一组装 ReActOutcome（各终止分支共用）。"""
        return ReActOutcome(
            success=success,
            content=content,
            reasoning=reasoning,
            structured=structured,
            tool_calls=(
                tool_calls if tool_calls is not None else self._tool_call_records
            ),
            iterations=iteration,
            total_tokens=total_tokens,
            usage=usage,
            error=error,
        )

    async def _dispatch(
        self, kind: AgentErrorKind, message: str, iteration: int
    ) -> AgentErrorAction:
        """错误分发：RAISE 抛 AgentRunError，否则返回 action（react.py 内 7 处分发唯一入口）。"""
        action = await self._error_handlers.dispatch(
            kind, AgentErrorContext(kind=kind, message=message, iteration=iteration)
        )
        if action == AgentErrorAction.RAISE:
            raise AgentRunError(kind, message, iteration)
        return action

    async def _finalize_llm_failed(
        self,
        stream_result: StreamResult,
        iteration: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """LLM 调用失败 → 错误分发（默认 STOP 短路；handler 可重试/上抛）。"""
        action = await self._dispatch(
            AgentErrorKind.LLM_FAILED, stream_result.error or "", iteration
        )
        if action == AgentErrorAction.CONTINUE:
            yield build_info_event(
                f"LLM 失败，按错误处理策略重试: {stream_result.error}"
            )
            return
        # STOP（默认）：短路失败
        self.outcome = self._build_outcome(
            success=False,
            content=stream_result.content,
            reasoning=stream_result.reasoning_content,
            iteration=iteration,
            total_tokens=total_usage.get("total_tokens", 0),
            usage=total_usage or None,
            error=stream_result.error,
        )
        yield build_done_event(
            iterations=iteration,
            total_tokens=total_usage.get("total_tokens", 0),
        )

    async def _handle_final_answer(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        iteration: int,
        output_schema: dict,
        total_usage: dict,
        full_reasoning: str,
    ) -> AsyncGenerator[str]:
        """final_answer 工具：成功提取终止 / 校验失败分发（CONTINUE 回喂）。

        主循环仅在检测到 final_answer 调用时才调用本方法。
        """
        final_tcs = [
            tc for tc in tool_calls if tc["function"]["name"] == _FINAL_ANSWER_TOOL
        ]
        structured, err = _extract_final_answer(final_tcs[0], output_schema)
        if structured is not None:
            # 成功：终止循环，结构化进 outcome
            self.outcome = self._build_outcome(
                success=True,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_tokens=total_usage.get("total_tokens", 0),
                usage=total_usage or None,
                structured=structured,
            )
            yield build_info_event("已收到结构化最终答案")
            yield build_done_event(
                iterations=iteration,
                total_tokens=total_usage.get("total_tokens", 0),
            )
            return

        # 校验失败 → 错误分发（默认 CONTINUE 回喂；handler 可终止/上抛）
        fa_msg = f"final_answer 参数{err}"
        action = await self._dispatch(
            AgentErrorKind.STRUCTURED_INVALID, fa_msg, iteration
        )

        if action == AgentErrorAction.STOP:
            self.outcome = self._build_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_tokens=total_usage.get("total_tokens", 0),
                usage=total_usage or None,
                error="final_answer 参数校验失败（按错误处理策略终止）",
            )
            yield build_done_event(
                iterations=iteration,
                total_tokens=total_usage.get("total_tokens", 0),
            )
            return

        # CONTINUE（默认）：回喂错误文本，模型下轮自纠
        messages.append(
            {
                "role": "tool",
                "tool_call_id": final_tcs[0].get("id", ""),
                "content": f"错误: {fa_msg}",
            }
        )
        self._tool_call_records.append(
            {
                "tool": _FINAL_ANSWER_TOOL,
                "params": {},
                "result": "",
                "success": False,
                "error": fa_msg,
                "error_code": ErrorCode.VALIDATION.value,
                "duration": 0.0,
            }
        )
        yield build_info_event(f"final_answer 校验失败，已回喂: {err}")

    async def _handle_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        iteration: int,
        total_usage: dict,
        full_reasoning: str,
    ) -> AsyncGenerator[str]:
        """工具执行 + 可恢复错误分发（默认 CONTINUE 继续）。

        上下文预算不在本方法内：统一在主循环顶部（每次 LLM 调用前）裁剪，
        所有继续路径（含非工具重试）共用，见 execute() 第 0 步。
        """
        before = len(self._tool_call_records)
        async for event in self.execute_tool_calls(tool_calls, messages, iteration):
            yield event

        # 可恢复错误分发：本轮失败工具按 kind 分组聚合后逐 kind 分发，
        # 再按「最严重优先」仲裁（RAISE > STOP > CONTINUE）——对齐 OpenAI 多失败优先级仲裁。
        # 终止/上报时其他失败不回喂模型（循环结束，回喂无意义），但全部失败已进证据链。
        new_failures = [
            r for r in self._tool_call_records[before:] if not r.get("success")
        ]
        if new_failures:
            grouped: dict[AgentErrorKind, list[dict]] = {}
            for r in new_failures:
                kind = (
                    AgentErrorKind.PARSE_FAILED
                    if r.get("error_code") == ErrorCode.JSON_PARSE.value
                    else AgentErrorKind.TOOL_FAILED
                )
                grouped.setdefault(kind, []).append(r)
            # 逐 kind 分发：同 kind 的多个失败聚合为一条 message（handler 可见全部原因）
            decisions: list[tuple[AgentErrorKind, str, AgentErrorAction]] = []
            for kind, fails in grouped.items():
                fail_msg = "；".join(
                    f"{f.get('tool', '?')}: {f.get('error', '')}" for f in fails
                )
                action = await self._dispatch(kind, fail_msg, iteration)
                decisions.append((kind, fail_msg, action))
            # 仲裁：任何 RAISE → 上报；任何 STOP → 终止；全 CONTINUE → 回喂继续
            for kind, fail_msg, action in decisions:
                if action == AgentErrorAction.RAISE:
                    raise AgentRunError(kind, fail_msg, iteration)
            for kind, fail_msg, action in decisions:
                if action == AgentErrorAction.STOP:
                    self.outcome = self._build_outcome(
                        success=False,
                        content="",
                        reasoning=full_reasoning.strip(),
                        iteration=iteration,
                        total_tokens=total_usage.get("total_tokens", 0),
                        usage=total_usage or None,
                        error=f"{kind.value}（按错误处理策略终止）: {fail_msg}",
                    )
                    yield build_done_event(
                        iterations=iteration,
                        total_tokens=total_usage.get("total_tokens", 0),
                    )
                    return
            # 全 CONTINUE：工具结果已回喂，继续循环

    async def _finalize_stop(
        self,
        full_reasoning: str,
        full_content: str,
        iteration: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """正常结束（stop/length/有内容）。"""
        self.outcome = self._build_outcome(
            success=bool(full_content.strip()),
            content=full_content.strip(),
            reasoning=full_reasoning.strip(),
            iteration=iteration,
            total_tokens=total_usage.get("total_tokens", 0),
            usage=total_usage or None,
        )
        yield build_done_event(
            iterations=iteration,
            total_tokens=total_usage.get("total_tokens", 0),
        )

    async def _handle_empty_output(
        self,
        full_reasoning: str,
        iteration: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """空输出 → 错误分发（默认 CONTINUE 重试；handler 可终止/上抛）。"""
        action = await self._dispatch(
            AgentErrorKind.EMPTY_OUTPUT, "LLM 未生成有效输出", iteration
        )
        if action == AgentErrorAction.STOP:
            self.outcome = self._build_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_tokens=total_usage.get("total_tokens", 0),
                usage=total_usage or None,
                error="LLM 未生成有效输出（按错误处理策略终止）",
            )
            yield build_done_event(
                iterations=iteration,
                total_tokens=total_usage.get("total_tokens", 0),
            )
            return
        # CONTINUE（默认）：重试
        yield build_info_event("LLM 未生成有效输出，重试")

    async def _finalize_max_turns(
        self,
        last_result: StreamResult | None,
        max_iterations: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """达到最大迭代次数 → 错误分发（默认 STOP 兜底；handler 可上抛）。"""
        await self._dispatch(
            AgentErrorKind.MAX_TURNS,
            f"已达到最大迭代次数({max_iterations})",
            max_iterations,
        )
        # STOP（默认）：现有兜底（CONTINUE 循环已耗尽，按 STOP 处理）
        yield build_info_event(f"已达到最大迭代次数({max_iterations})")
        if last_result:
            self.outcome = self._build_outcome(
                success=bool(last_result.content.strip()),
                content=last_result.content.strip(),
                reasoning=last_result.reasoning_content.strip(),
                iteration=max_iterations,
                total_tokens=total_usage.get("total_tokens", 0),
                usage=total_usage or None,
            )
        else:
            # 防御分支（last_result 恒非 None），与既有行为一致（空默认）
            self.outcome = ReActOutcome(
                success=False,
                content="",
                iterations=max_iterations,
                error="LLM 未返回任何结果",
            )
        yield build_done_event(
            iterations=max_iterations,
            total_tokens=total_usage.get("total_tokens", 0),
        )

    async def _finalize_timeout(
        self,
        last_result: StreamResult | None,
        iteration: int,
        total_usage: dict,
        max_execution_time: float | None,
    ) -> AsyncGenerator[str]:
        """总时长超时 → 错误分发（默认 STOP 降级；handler 可上抛）。"""
        await self._dispatch(
            AgentErrorKind.TIMEOUT,
            f"ReAct 执行超时（超过 {max_execution_time} 秒）",
            iteration,
        )
        # STOP（默认）：对齐 max_iterations 兜底，用 last_result 组装降级 outcome
        error = f"ReAct 执行超时（超过 {max_execution_time} 秒）"
        self.outcome = self._build_outcome(
            success=bool(last_result.content.strip()) if last_result else False,
            content=last_result.content.strip() if last_result else "",
            reasoning=last_result.reasoning_content.strip() if last_result else "",
            iteration=iteration,
            total_tokens=total_usage.get("total_tokens", 0),
            usage=total_usage or None,
            error=error,
        )
        yield build_info_event(error)
        yield build_done_event(
            iterations=self.outcome.iterations,
            total_tokens=self.outcome.total_tokens,
        )

    async def _finalize_cost_exceeded(
        self,
        last_result: StreamResult | None,
        iteration: int,
        total_usage: dict,
        cost: float,
    ) -> AsyncGenerator[str]:
        """累计成本超限 → 错误分发（默认 STOP 停机；handler 可上抛）。

        对齐 _finalize_timeout 降级模式：dispatch → build_outcome + info + done；
        CONTINUE 被忽略（终结性护栏，同 TIMEOUT/MAX_TURNS），RAISE 由 _dispatch 抛出。
        cost_limiter 为 None 时主循环不进入本方法。
        """
        error = f"ReAct 执行成本超限（累计 ${cost:.4f}）"
        await self._dispatch(AgentErrorKind.COST_EXCEEDED, error, iteration)
        # STOP（默认）：用 last_result 组装降级 outcome（有 content 算部分成功）。
        # last_result 在主循环该检查点必非 None（累加在赋值后），None 分支防御性对齐。
        self.outcome = self._build_outcome(
            success=bool(last_result.content.strip()) if last_result else False,
            content=last_result.content.strip() if last_result else "",
            reasoning=last_result.reasoning_content.strip() if last_result else "",
            iteration=iteration,
            total_tokens=total_usage.get("total_tokens", 0),
            usage=total_usage or None,
            error=error,
        )
        yield build_info_event(error)
        yield build_done_event(
            iterations=self.outcome.iterations,
            total_tokens=self.outcome.total_tokens,
        )

    async def execute_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        iteration: int,
    ) -> AsyncGenerator[str]:
        """
        并行执行工具调用列表，追加结果到 messages，记录到 _tool_call_records。

        并发执行：asyncio.gather 并行执行所有工具（并发度由 ToolService 的
        工具级信号量 agent_max_concurrent_tools 限制）。gather 保证结果顺序 =
        输入顺序，因此 tool_messages / _tool_call_records 的顺序与 tool_calls
        一致——OpenAI 兼容 API 要求 tool 消息与前置 assistant.tool_calls 的
        tool_call_id 配对，顺序不能乱。

        独立使用场景：PlannerAgent 执行阶段（程序执行计划步骤的工具）、
        ReflectionAgent 收集阶段。此时调用方需自行读取结果（tool 消息已写入 messages）。

        SSE 事件只在主 generator 内按顺序 yield（不在并发 task 内 yield，
        避免事件交错）。

        Yields:
            tool_call / tool_result SSE 事件
        """

        async def _execute_one(tc: dict) -> tuple:
            """并行执行单个工具（并发 task 内只做执行，不 yield 事件）。"""
            tool_name = tc["function"]["name"]

            try:
                raw_args = tc["function"]["arguments"]
                tool_args = json.loads(raw_args)
            except (json.JSONDecodeError, KeyError) as e:
                # 参数 JSON 解析失败：不静默用空参执行（会掩盖错误、可能触发副作用），
                # 构造失败 ToolResult 走失败回喂分支——模型可见原因自纠，JSON_PARSE 进证据链。
                raw_args = tc.get("function", {}).get("arguments", "")
                start = time.monotonic()
                exec_result = ToolResult(
                    success=False,
                    content="",
                    error=f"参数 JSON 解析失败: {e!s}（原始参数: {raw_args[:200]}）",
                    error_code=ErrorCode.JSON_PARSE,
                )
                elapsed = time.monotonic() - start
                return exec_result, tool_name, {}, tc, elapsed

            start = time.monotonic()
            exec_result = await self._tools.execute(tool_name, tool_args)
            elapsed = time.monotonic() - start
            return exec_result, tool_name, tool_args, tc, elapsed

        # gather 保证结果顺序 = tool_calls 输入顺序
        results = await asyncio.gather(*[_execute_one(tc) for tc in tool_calls])

        tool_messages: list[dict] = []
        for exec_result, tool_name, tool_args, tc, elapsed in results:
            yield build_tool_call_event(tool_name, tool_args, iteration)

            # 回喂模型：成功回喂 content，失败回喂 str(result)（"错误: <error>"）——
            # 模型需看到失败原因才能自愈（工具失败空串回喂是核心缺口）。
            # error / error_code 同时进证据链记录（根因报告要能看到失败原因与分类）。
            feedback = exec_result.content if exec_result.success else str(exec_result)

            self._tool_call_records.append(
                {
                    "tool": tool_name,
                    "params": tool_args,
                    "result": exec_result.content,
                    "success": exec_result.success,
                    "error": exec_result.error,
                    "error_code": (
                        exec_result.error_code.value if exec_result.error_code else None
                    ),
                    "duration": round(elapsed, 3),
                }
            )

            yield build_tool_result_event(
                tool_name,
                _truncate_with_marker(feedback, 200),
                elapsed,
                iteration,
            )

            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": _truncate_with_marker(feedback, 2000),
                }
            )

        messages.extend(tool_messages)
