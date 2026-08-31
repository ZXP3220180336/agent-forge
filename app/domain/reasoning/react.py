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
import logging
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

# 策略层标准库日志（对齐「只依赖 ports + shared + 标准库」依赖方向，不用 platform 的
# get_logger）；logger 名对齐 app.* 命名空间，可被 setup_logging 的 handler 捕获。
_logger = logging.getLogger("app.domain.reasoning.react")

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


def _action_fingerprint(tool_calls: list[dict]) -> str:
    """工具调用动作指纹：本轮所有工具（名 + 规范化参数）序列化，供停滞检测。

    参数 json.loads 后 sort_keys 重 dump——语义相同的不同 key 顺序 / 空白指纹一致
    （对齐 ml-intern doom-loop args 规范化）；参数 JSON 非法时回退原始字符串。
    排除 final_answer（终止工具，非循环动作）；整轮仅 final_answer 时返回空串（不检测）。
    """
    sig = []
    for tc in tool_calls:
        name = tc["function"]["name"]
        if name == _FINAL_ANSWER_TOOL:
            continue
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = tc.get("function", {}).get("arguments", "")
        sig.append((name, args))
    return json.dumps(sig, sort_keys=True, ensure_ascii=False, default=str)


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
        self._error_handlers = error_handlers or ErrorHandlerRegistry()
        self._context_budget = context_budget
        self._cost_limiter = cost_limiter
        self._tool_call_records: list[dict[str, Any]] = []
        # 连续空输出重试计数（execute 每次开头重置；本轮有产出清零、空输出 +1）
        self._empty_retries = 0
        # 循环停滞检测状态：上一轮动作指纹 + 连续相同计数（execute 开头重置）
        self._last_action_fp: str | None = None
        self._stall_count = 0
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
        max_empty_retries: int = 2,
        max_llm_fail_retries: int = 2,
        max_same_action_turns: int = 3,
        output_schema: dict | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[str]:
        """
        ReAct 主循环。

        循环流程（各分支经错误处理分发，默认行为 = 现有逻辑）：
            1. 用户取消（cancel_event 置位）→ CANCELLED 分发（优雅停止，不开始新轮）
            2. 上下文预算裁剪（所有继续路径共用，保证每次 LLM 调用前消息有界）
            3. LLM 推理（流式输出 reasoning / message，cancel_event 传给 LLM 层中断调用）
            4. 成本护栏：累计成本超限 → COST_EXCEEDED 分发（默认停机降级）
            5. LLM 失败（stream_result.error 非空）：取消置位 → CANCELLED；否则 LLM_FAILED 分发
               （默认 STOP 短路；handler 可 CONTINUE 重试，重试受 max_llm_fail_retries 上限硬终止）
            6. 模型拒答（refusal 字段 / content_filter）→ REFUSED 分发（默认停机）
            7. 追加 assistant 消息（reasoning_content 按 has_reasoning 回喂 + tool_calls 配对，防 400）
            8. 检查 finish_reason
               - "tool_calls" 但无 tool_calls / 无工具可用 → 协议异常 → PARSE_FAILED 分发（默认重试，不入空输出计数）
               - "tool_calls" → final_answer 检测 → 停滞检测（连续相同超限硬终止）→ 执行工具，追加结果，继续循环
               - "stop"       → 生成最终结果，结束循环
               - "length"     → 生成部分结果，结束循环
               - 空输出       → 连续计数 +1；超过 max_empty_retries 硬终止，否则错误分发重试
            9. 循环耗尽 → MAX_TURNS 分发（默认兜底）
            10. 超时（总时长上限）→ TIMEOUT 分发（默认超时降级）
            11. 未捕获异常 → UNKNOWN 分发（默认保留部分进度）；AgentRunError（RAISE 决策）前置 re-raise

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
            max_empty_retries: 连续空输出重试上限——空输出最多重试 N 次，第
                N+1 次仍空输出则终止（0=首次空输出即终止）；达上限走 EMPTY_OUTPUT
                分发硬终止（防模型空转烧钱）
            max_llm_fail_retries: LLM 失败重试上限——LLM 调用失败最多重试 N 次，第
                N+1 次仍失败则终止（0=首次失败即终止；对齐空输出护栏）；达上限走
                LLM_FAILED 分发硬终止——即使 handler 返回 CONTINUE 也不继续（防
                handler 配置失误 / LLM 持续失败时无限重试烧钱）
            max_same_action_turns: 循环停滞检测——连续相同工具调用（工具+参数）
                超过 N 轮后，下一轮仍相同则终止（默认 3）；达上限走 STALLED
                分发硬终止（防死循环烧钱/重复副作用）
            output_schema: 最终答案结构化 JSON Schema（None=不启用）。启用时注入
                final_answer 工具，模型最后调用提交结构化结果并终止循环
            cancel_event: 优雅取消信号（asyncio.Event，None=不启用）——置位时在轮次
                边界停止（对齐 OpenAI after_turn）；主循环顶部 + LLM error 分支识别
                → CANCELLED 分发（不重试），保留部分进度；同时传给 LLM 层中断调用

        Yields:
            SSE 事件字符串（reasoning / message / tool_call / tool_result / info / done）
        """
        tool_defs = self._tools.get_openai_tools() if self._tools else None
        # 结构化最终答案：注入 final_answer 工具（模型最后调用提交结构化结果并终止）
        if output_schema is not None:
            tool_defs = [*(tool_defs or []), _build_final_answer_tool(output_schema)]
        has_tools = bool(tool_defs)

        self._tool_call_records = []
        # 连续空输出重试计数：execute 每次独立（有产出清零 / 空输出 +1，见主循环）
        self._empty_retries = 0
        # LLM 失败重试计数：execute 每次独立（成功轮清零 / 失败轮 +1，见主循环；
        # 对齐空输出护栏，防 handler CONTINUE 无限重试烧钱）
        self._llm_fail_retries = 0
        # 循环停滞检测：execute 每次独立（相同动作指纹 + 连续计数，见主循环）
        self._last_action_fp = None
        self._stall_count = 0
        last_result: StreamResult | None = None
        total_usage: dict = {}
        # 记录进入 timeout 的 task：超时降级时判别「真超时」与「生成器被 finalizer
        # 关闭」（慢消费者场景 aclose 由不同 task 驱动，需干净停止不 yield 降级事件）。
        entered_task = asyncio.current_task()

        try:
            async with asyncio.timeout(max_execution_time):
                for iteration in range(1, max_iterations + 1):
                    # ----- 1. 用户取消（cancel_event 置位）→ CANCELLED 分发（优雅停止）-----
                    # 置于每轮 LLM 调用前：快速响应（即使不在 LLM 调用中，如工具执行后）；
                    # 不开始新轮。对齐 OpenAI after_turn 优雅取消语义。
                    if cancel_event is not None and cancel_event.is_set():
                        async for event in self._finalize_cancelled(
                            last_result, iteration, total_usage
                        ):
                            yield event
                        return

                    yield build_info_event(f"第 {iteration} 轮推理")

                    # ----- 2. 上下文预算：模型本次调用前作为 gatekeeper 裁剪 -----
                    # 置于循环顶部（而非工具路径后）：所有继续路径共用——工具回喂、
                    # LLM 失败重试 / final_answer 回喂重试 / 空输出重试，下一次 LLM
                    # 调用前均裁剪，否则非工具路径上下文无限增长、预算失效。
                    if self._context_budget is not None:
                        self._context_budget.trim_messages(
                            messages,
                            max_rounds=max_context_rounds,
                            max_tokens=max_context_tokens,
                        )

                    # ----- 3. LLM 推理 -----
                    stream_result = StreamResult()

                    async for event in self._llm.async_generate(
                        messages=messages,
                        tools=tool_defs,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        result=stream_result,
                        cancel_event=cancel_event,
                    ):
                        yield event

                    last_result = stream_result
                    # 累计 token 用量
                    if stream_result.usage:
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            total_usage[k] = total_usage.get(
                                k, 0
                            ) + stream_result.usage.get(k, 0)

                    # ----- 4. 成本护栏：累计成本超限 → 错误分发（默认 STOP 降级）-----
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

                    # ----- 5. LLM 失败（stream_result.error 非空）→ 取消判定 / LLM_FAILED 分发 -----
                    # 短路返回失败结果，不把「失败」当「空输出」继续空转重试（浪费 LLM 调用 + 错误信息不准确）。
                    # 正常空回（stop + 空 content）error 为 None，仍走下方「空输出重试」逻辑。
                    if stream_result.error:
                        # 用户取消（cancel_event 置位，LLM 调用被中断）→ CANCELLED 分发
                        # （不重试——取消是用户意图，重试无意义；取消不参与失败重试计数）
                        if cancel_event is not None and cancel_event.is_set():
                            async for event in self._finalize_cancelled(
                                last_result, iteration, total_usage
                            ):
                                yield event
                            return
                        # 连续 LLM 失败重试计数（对齐空输出护栏）：失败轮 +1，
                        # 成功轮清零（见下方）。取消不计数（上方已 return）。
                        self._llm_fail_retries += 1
                        # LLM 调用失败 → 错误分发（默认 STOP 短路；handler 可重试/上抛，
                        # 重试受 max_llm_fail_retries 上限硬终止）
                        async for event in self._finalize_llm_failed(
                            stream_result, iteration, total_usage, max_llm_fail_retries
                        ):
                            yield event
                        if self.outcome is not None:
                            return
                        continue  # CONTINUE：重试
                    # LLM 成功轮（error 为 None）→ 失败重试计数清零（对齐空输出「有产出清零」）
                    self._llm_fail_retries = 0

                    # ----- 6. 模型拒答（refusal 字段 / content_filter）→ REFUSED 分发（默认 STOP）-----
                    # 显式拒答信号（LLM-004 原则：拒答基于显式信号，不靠 content 空推断）；
                    # 拒答终止不误判为成功答案、不空转重试。DeepSeek stop+空 content 属
                    # 空回答（非显式拒答），保持现有 _finalize_stop 语义。
                    if (
                        stream_result.refusal
                        or stream_result.finish_reason == "content_filter"
                    ):
                        async for event in self._finalize_refused(
                            stream_result, iteration, total_usage
                        ):
                            yield event
                        return

                    full_reasoning = stream_result.reasoning_content
                    full_content = stream_result.content

                    # ----- 7. 将 LLM 回复追加到消息历史 -----
                    # 纯空轮（无 content / 无 reasoning / 无 tool_calls / 无 has_reasoning
                    # 信号）不追加——空 assistant 消息无信息量，空输出重试累积会污染上下文
                    # （模型下轮看不到空消息也无影响；对齐工业级不把空输出轮写进历史）。
                    # has_reasoning 保留在条件内：thinking 模型返回空 reasoning 也追加
                    # （防 400 回喂字段需要，见 reasoning_content 回喂节）。
                    if (
                        full_content
                        or full_reasoning
                        or stream_result.has_reasoning
                        or stream_result.tool_calls
                    ):
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

                    # ----- 8. 根据 finish_reason 决定下一步 -----
                    finish_reason = stream_result.finish_reason or ""
                    if finish_reason == "tool_calls":
                        # ----- （1）调用工具 → 协议异常重试或协议正常继续调用大模型
                        if not stream_result.tool_calls or not has_tools:
                            # 协议异常：信号与数据/工具可用性不一致
                            # 两类不一致：
                            # ① 声明调工具却没给出 tool_calls（服务端异常/被截断）；
                            # ② 要调工具但系统未注册任何工具（has_tools=False——模型选了工具而
                            #    注册表为空，协议不一致）。
                            # - 均非「空输出」（有调用意图）、非「工具失败」（无工具可执行）。
                            # - 短路为 PARSE_FAILED 分发
                            # - 不入空输出计数（与 max_empty_retries 独立，
                            #   避免 tool_calls 为真清零计数后无上限空转）
                            # - 不进 execute_tool_calls 空转。
                            # - 默认 CONTINUE 重试，handler 可 STOP/RAISE。
                            async for event in self._finalize_protocol_error(
                                full_reasoning, iteration, total_usage
                            ):
                                yield event
                            if self.outcome is not None:
                                return
                            continue  # CONTINUE（默认）：重试下一轮
                        else:
                            # 协议正常：继续调用大模型
                            self._empty_retries = (
                                0  # 本轮有产出（正常工具调用） → 空输出连续计数清零；
                            )
                            yield build_info_event(
                                f"检测到 {len(stream_result.tool_calls)} 个工具调用"
                            )

                            # ----- Final Answer 工具：结构化最终答案 → 提取终止 / 回喂继续 -----
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

                            # ----- 循环停滞检测：相同工具 + 参数连续重复 → STALLED 分发硬终止 -----
                            # 动作指纹 = 本轮 tool_calls 的（名, 规范化参数）序列化；连续相同
                            # 超过 max_same_action_turns 轮判死循环（对齐 SMOL same_action_llm_turn_limit）。
                            # 停滞判定后不执行本轮工具：执行无意义 + 防重复副作用 / 烧钱。
                            fp = _action_fingerprint(stream_result.tool_calls)
                            if fp and fp == self._last_action_fp:
                                self._stall_count += 1
                            else:
                                self._stall_count = 1
                                self._last_action_fp = fp
                            if self._stall_count > max_same_action_turns:
                                async for event in self._finalize_stalled(
                                    stream_result.tool_calls,
                                    iteration,
                                    total_usage,
                                    full_reasoning,
                                    self._stall_count,
                                ):
                                    yield event
                                return

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
                    elif finish_reason in ("stop", "length") or full_content.strip():
                        # ----- （2）stop / length / 有内容 → 正常结束
                        self._empty_retries = 0  # 本轮有产出（stop / length / 有内容）→ 空输出连续计数清零；
                        async for event in self._finalize_stop(
                            full_reasoning, full_content, iteration, total_usage
                        ):
                            yield event
                        return
                    else:
                        # ----- （3）空输出 → 错误分发（默认 CONTINUE 重试；handler 可终止/上抛）
                        self._empty_retries += 1  # 本轮空输出 → 空输出连续计数+1；
                        async for event in self._handle_empty_output(
                            full_reasoning, iteration, total_usage, max_empty_retries
                        ):
                            yield event
                        if self.outcome is not None:
                            return
                        # CONTINUE（默认）：重试（_handle_empty_output 已 yield 重试信息）

                # ----- 9. 达到最大迭代次数 → 错误分发（默认 STOP 兜底；handler 可上抛） -----
                async for event in self._finalize_max_turns(
                    last_result, max_iterations, total_usage
                ):
                    yield event

        except AgentRunError:
            # RAISE 决策的领域错误：不吞，传播到 BaseAgent.run（统一 re-raise），
            # 不被下方 except Exception 兜底转 UNKNOWN
            raise
        except TimeoutError:
            # ----- 10. 超时（总时长上限）→ TIMEOUT 分发（默认超时降级）-----
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

        except Exception as e:  # noqa: BLE001 — 未捕获异常 → UNKNOWN 分发（保留部分进度）
            # ----- 11. 未捕获异常 → UNKNOWN 分发（默认保留部分进度）-----
            # 关闭判别（对齐 TimeoutError 分支）：生成器被 finalizer/aclose 关闭
            # 或外部取消 → 干净停止，不 yield 降级事件
            cur = asyncio.current_task()
            if cur is None or cur is not entered_task or cur.cancelling() > 0:
                return

            # 真异常 → UNKNOWN 分发（默认 STOP，保留 last_result 部分进度 + 证据链）。
            # asyncio.CancelledError / GeneratorExit 是 BaseException，不被本分支捕获
            # → 保持 CANCELLED / 生成器关闭语义不变。
            async for event in self._finalize_unknown(
                last_result, iteration, total_usage, e
            ):
                yield event

            return

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

    # ==================================================================
    # execute 的拆分方法（职责单一，行为与原内联分支一致）
    # 终止信号：设置 self.outcome = 终止；不设置 = 继续循环（主循环据此 return/continue）
    # ==================================================================

    async def _dispatch(
        self, kind: AgentErrorKind, message: str, iteration: int
    ) -> AgentErrorAction:
        """错误分发：RAISE 抛 AgentRunError，否则返回 action（react.py 内 13 处分发唯一入口）。"""
        action = await self._error_handlers.dispatch(
            kind, AgentErrorContext(kind=kind, message=message, iteration=iteration)
        )
        if action == AgentErrorAction.RAISE:
            raise AgentRunError(kind, message, iteration)
        return action

    async def _finalize_outcome(
        self,
        *,
        success: bool,
        content: str,
        reasoning: str,
        iteration: int,
        total_usage: dict,
        error: str | None = None,
        info_message: str | None = None,
        structured: dict | None = None,
    ) -> AsyncGenerator[str]:
        """统一收尾：组装 outcome + 产出事件（可选 info + done 恰一次）。

        覆盖各终结 / STOP 分支的公共尾段（dispatch 由调用方负责——CONTINUE 语义
        各异：重试 / 回喂 / 忽略）。done 的 iterations / total_tokens 从 outcome 取。
        原 _build_outcome（统一组装结果载体）已并入本方法——无独立消费方。
        """
        self.outcome = ReActOutcome(
            success=success,
            content=content,
            reasoning=reasoning,
            structured=structured,
            tool_calls=self._tool_call_records,
            iterations=iteration,
            total_tokens=total_usage.get("total_tokens", 0),
            usage=total_usage or None,
            error=error,
        )
        if info_message:
            yield build_info_event(info_message)
        yield build_done_event(
            iterations=self.outcome.iterations,
            total_tokens=self.outcome.total_tokens,
        )

    async def _finalize_llm_failed(
        self,
        stream_result: StreamResult,
        iteration: int,
        total_usage: dict,
        max_llm_fail_retries: int,
    ) -> AsyncGenerator[str]:
        """LLM 调用失败 → 错误分发（默认 STOP 短路；handler 可重试/上抛）。

        CONTINUE 重试受 max_llm_fail_retries 上限护栏（对齐空输出）：连续失败超上限
        后硬终止——即使 handler 返回 CONTINUE 也不继续（终结护栏，防 handler 配置
        失误 / LLM 持续失败时无限重试烧钱）。
        """
        # 硬终止分支：先 dispatch（handler 可 RAISE 上抛），STOP/CONTINUE 均终止
        if self._llm_fail_retries > max_llm_fail_retries:
            error = f"连续 LLM 调用失败（{self._llm_fail_retries} 轮），已终止"
            await self._dispatch(AgentErrorKind.LLM_FAILED, error, iteration)
            async for event in self._finalize_outcome(
                success=False,
                content=stream_result.content,
                reasoning=stream_result.reasoning_content,
                iteration=iteration,
                total_usage=total_usage,
                error=error,
                info_message=error,
            ):
                yield event
            return

        action = await self._dispatch(
            AgentErrorKind.LLM_FAILED, stream_result.error or "", iteration
        )
        if action == AgentErrorAction.CONTINUE:
            yield build_info_event(
                f"LLM 失败，按错误处理策略重试: {stream_result.error}"
            )
            return
        # STOP（默认）：短路失败
        async for event in self._finalize_outcome(
            success=False,
            content=stream_result.content,
            reasoning=stream_result.reasoning_content,
            iteration=iteration,
            total_usage=total_usage,
            error=stream_result.error,
        ):
            yield event

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
            async for event in self._finalize_outcome(
                success=True,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                structured=structured,
                info_message="已收到结构化最终答案",
            ):
                yield event
            return

        # 校验失败 → 错误分发（默认 CONTINUE 回喂；handler 可终止/上抛）
        fa_msg = f"final_answer 参数{err}"
        action = await self._dispatch(
            AgentErrorKind.STRUCTURED_INVALID, fa_msg, iteration
        )

        if action == AgentErrorAction.STOP:
            async for event in self._finalize_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                error="final_answer 参数校验失败（按错误处理策略终止）",
            ):
                yield event
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
                    async for event in self._finalize_outcome(
                        success=False,
                        content="",
                        reasoning=full_reasoning.strip(),
                        iteration=iteration,
                        total_usage=total_usage,
                        error=f"{kind.value}（按错误处理策略终止）: {fail_msg}",
                    ):
                        yield event
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
        async for event in self._finalize_outcome(
            success=bool(full_content.strip()),
            content=full_content.strip(),
            reasoning=full_reasoning.strip(),
            iteration=iteration,
            total_usage=total_usage,
        ):
            yield event

    async def _finalize_protocol_error(
        self,
        full_reasoning: str,
        iteration: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """协议异常（finish_reason=tool_calls 但未返回工具调用）→ PARSE_FAILED 分发。

        模型声明要调工具却没给出 tool_calls——协议信号不一致（服务端异常/被截断），
        非「空输出」（有调用意图）、非「工具失败」（无工具可执行）。默认 CONTINUE
        重试下一轮（不参与空输出计数 / 停滞检测）；handler 可 STOP 终止 / RAISE 上抛。
        """
        msg = "finish_reason=tool_calls 但未返回工具调用（协议异常）"
        action = await self._dispatch(AgentErrorKind.PARSE_FAILED, msg, iteration)
        if action == AgentErrorAction.CONTINUE:
            yield build_info_event(f"{msg}，按错误处理策略重试")
            return
        # STOP（默认）：终止（error 记录协议异常）
        async for event in self._finalize_outcome(
            success=False,
            content="",
            reasoning=full_reasoning.strip(),
            iteration=iteration,
            total_usage=total_usage,
            error=msg,
        ):
            yield event

    async def _handle_empty_output(
        self,
        full_reasoning: str,
        iteration: int,
        total_usage: dict,
        max_empty_retries: int,
    ) -> AsyncGenerator[str]:
        """空输出 → 错误分发（默认 CONTINUE 重试；handler 可终止/上抛）。

        连续空输出重试上限：计数超过 max_empty_retries 后硬终止——即使 handler 返回
        CONTINUE 也不继续（终结护栏，对齐 TIMEOUT / COST_EXCEEDED），防模型空转烧钱。
        """
        # 硬终止分支：先 dispatch（handler 可 RAISE 上抛），STOP/CONTINUE 均终止
        if self._empty_retries > max_empty_retries:
            error = f"连续空输出（{self._empty_retries} 轮），已终止"
            await self._dispatch(AgentErrorKind.EMPTY_OUTPUT, error, iteration)
            async for event in self._finalize_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                error=error,
                info_message=error,
            ):
                yield event
            return

        action = await self._dispatch(
            AgentErrorKind.EMPTY_OUTPUT, "LLM 未生成有效输出", iteration
        )
        if action == AgentErrorAction.STOP:
            async for event in self._finalize_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                error="LLM 未生成有效输出（按错误处理策略终止）",
                info_message="LLM 未生成有效输出（按错误处理策略终止）",
            ):
                yield event
            return

        # CONTINUE（默认）：重试
        yield build_info_event("LLM 未生成有效输出，重试")

    async def _finalize_terminal(
        self,
        kind: AgentErrorKind,
        message: str,
        iteration: int,
        *,
        success: bool,
        content: str,
        reasoning: str,
        total_usage: dict,
        error: str | None = None,
        info_message: str | None = None,
        structured: dict | None = None,
    ) -> AsyncGenerator[str]:
        """终结性护栏统一收尾：dispatch（RAISE 上抛，CONTINUE 忽略）→ 收尾。

        供「CONTINUE 无重试语义」的终结分支（TIMEOUT / COST_EXCEEDED / STALLED /
        MAX_TURNS）复用；可恢复分支（CONTINUE 有重试 / 回喂语义，需先拿 action
        分派）用 _finalize_outcome。
        """
        await self._dispatch(kind, message, iteration)
        async for event in self._finalize_outcome(
            success=success,
            content=content,
            reasoning=reasoning,
            iteration=iteration,
            total_usage=total_usage,
            error=error,
            info_message=info_message,
            structured=structured,
        ):
            yield event

    async def _finalize_max_turns(
        self,
        last_result: StreamResult | None,
        max_iterations: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """达到最大迭代次数 → 错误分发（默认 STOP 兜底；handler 可上抛）。"""
        error = f"已达到最大迭代次数({max_iterations})"
        # STOP（默认）：现有兜底（CONTINUE 循环已耗尽，按 STOP 处理）
        async for event in self._finalize_terminal(
            AgentErrorKind.MAX_TURNS,
            error,
            max_iterations,
            success=bool(last_result.content.strip()) if last_result else False,
            content=last_result.content.strip() if last_result else "",
            reasoning=last_result.reasoning_content.strip() if last_result else "",
            total_usage=total_usage,
            error=error,
            info_message=error,
        ):
            yield event

    async def _finalize_timeout(
        self,
        last_result: StreamResult | None,
        iteration: int,
        total_usage: dict,
        max_execution_time: float | None,
    ) -> AsyncGenerator[str]:
        """总时长超时 → 错误分发（默认 STOP 降级；handler 可上抛）。"""
        error = f"ReAct 执行超时（超过 {max_execution_time} 秒）"
        # STOP（默认）：对齐 max_iterations 兜底，用 last_result 组装降级 outcome
        async for event in self._finalize_terminal(
            AgentErrorKind.TIMEOUT,
            error,
            iteration,
            success=bool(last_result.content.strip()) if last_result else False,
            content=last_result.content.strip() if last_result else "",
            reasoning=last_result.reasoning_content.strip() if last_result else "",
            total_usage=total_usage,
            error=error,
            info_message=error,
        ):
            yield event

    async def _finalize_cost_exceeded(
        self,
        last_result: StreamResult | None,
        iteration: int,
        total_usage: dict,
        cost: float,
    ) -> AsyncGenerator[str]:
        """累计成本超限 → 错误分发（默认 STOP 停机；handler 可上抛）。

        终结护栏统一经 _finalize_terminal 收尾（dispatch → outcome + info + done）；
        CONTINUE 被忽略（同 TIMEOUT / STALLED），RAISE 由 _dispatch 抛出。
        cost_limiter 为 None 时主循环不进入本方法。
        """
        error = f"ReAct 执行成本超限（累计 ${cost:.4f}）"
        # 用 last_result 组装降级 outcome（有 content 算部分成功）。
        # last_result 在主循环该检查点必非 None（累加在赋值后），None 分支防御性对齐。
        async for event in self._finalize_terminal(
            AgentErrorKind.COST_EXCEEDED,
            error,
            iteration,
            success=bool(last_result.content.strip()) if last_result else False,
            content=last_result.content.strip() if last_result else "",
            reasoning=last_result.reasoning_content.strip() if last_result else "",
            total_usage=total_usage,
            error=error,
            info_message=error,
        ):
            yield event

    async def _finalize_stalled(
        self,
        tool_calls: list[dict],
        iteration: int,
        total_usage: dict,
        full_reasoning: str,
        count: int,
    ) -> AsyncGenerator[str]:
        """连续相同工具调用超限 → 错误分发（默认 STOP 停机；handler 可上抛）。

        终结护栏统一经 _finalize_terminal 收尾（dispatch → outcome + info + done）；
        CONTINUE 被忽略（同 TIMEOUT / COST_EXCEEDED），RAISE 由 _dispatch 抛出。
        检测点在工具执行前：本方法返回即终止，本轮工具不执行。
        """
        names = "、".join(
            tc["function"]["name"]
            for tc in tool_calls
            if tc["function"]["name"] != _FINAL_ANSWER_TOOL
        )
        error = f"连续 {count} 轮相同工具调用（{names}），已终止"
        # 停机组装 outcome（本轮无工具执行，content 空，保留 reasoning）
        async for event in self._finalize_terminal(
            AgentErrorKind.STALLED,
            error,
            iteration,
            success=False,
            content="",
            reasoning=full_reasoning.strip(),
            total_usage=total_usage,
            error=error,
            info_message=error,
        ):
            yield event

    async def _finalize_refused(
        self,
        stream_result: StreamResult,
        iteration: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """模型拒答 → 错误分发（默认 STOP；不误判为成功答案、不空转重试）。

        显式拒答信号（refusal 字段 / content_filter，LLM-004 原则）。拒答文本
        截断（LLM-008 基线：拒答常引用触发内容，完整文本不落盘）。复用
        _finalize_terminal 终结护栏（CONTINUE 忽略，RAISE 由 _dispatch 抛出）。
        """
        reason = stream_result.refusal or "内容安全策略触发（content_filter）"
        error = f"模型拒答: {reason[:200]}"
        async for event in self._finalize_terminal(
            AgentErrorKind.REFUSED,
            error,
            iteration,
            success=False,
            content=stream_result.content,
            reasoning=stream_result.reasoning_content,
            total_usage=total_usage,
            error=error,
            info_message=error,
        ):
            yield event

    async def _finalize_unknown(
        self,
        last_result: StreamResult | None,
        iteration: int,
        total_usage: dict,
        exc: Exception,
    ) -> AsyncGenerator[str]:
        """未捕获异常 → 错误分发（默认 STOP；保留部分进度）。

        对齐 _finalize_timeout 降级：用 last_result 组装 outcome（保留已执行工具
        证据链 + 部分内容），与 TIMEOUT / COST_EXCEEDED / STALLED 的「部分进度保留」
        模式一致。复用 _finalize_terminal 终结护栏（CONTINUE 忽略，RAISE 由 _dispatch
        抛出）。asyncio.CancelledError / GeneratorExit 是 BaseException，不被主循环
        except Exception 捕获（保持 CANCELLED / 生成器关闭语义）。

        error 脱敏：只保留异常类型名（分类），不拼接异常 message——异常文本可能含
        内部路径 / 参数 / 敏感值 / 堆栈提示，产品可见文本（outcome.error / SSE /
        根因报告）与运维诊断分离：完整异常（含 traceback）进日志，不落产品侧。
        """
        _logger.error("Agent 运行异常: %s", exc, exc_info=exc)
        error = f"Agent 运行异常: {type(exc).__name__}"
        async for event in self._finalize_terminal(
            AgentErrorKind.UNKNOWN,
            error,
            iteration,
            success=bool(last_result.content.strip()) if last_result else False,
            content=last_result.content.strip() if last_result else "",
            reasoning=last_result.reasoning_content.strip() if last_result else "",
            total_usage=total_usage,
            error=error,
            info_message=error,
        ):
            yield event

    async def _finalize_cancelled(
        self,
        last_result: StreamResult | None,
        iteration: int,
        total_usage: dict,
    ) -> AsyncGenerator[str]:
        """用户取消（cancel_event 置位）→ 错误分发（默认 STOP；保留部分进度）。

        优雅取消（对齐 OpenAI after_turn）：轮次边界停止，保留已执行工具证据链 +
        部分内容（对齐 UNKNOWN / TIMEOUT 部分进度保留模式）。复用 _finalize_terminal
        终结护栏（CONTINUE 忽略，RAISE 由 _dispatch 抛出）。
        """
        error = "Agent 已被取消"
        async for event in self._finalize_terminal(
            AgentErrorKind.CANCELLED,
            error,
            iteration,
            success=bool(last_result.content.strip()) if last_result else False,
            content=last_result.content.strip() if last_result else "",
            reasoning=last_result.reasoning_content.strip() if last_result else "",
            total_usage=total_usage,
            error=error,
            info_message=error,
        ):
            yield event
