# ============================================
# domain/reasoning/react.py - ReAct 推理策略实现
# ============================================
"""
ReAct 推理策略（ReActStrategy）
==============================

推理（Reason）→ 行动（Act）→ 观察（Observe），循环直到完成。

本模块是领域层推理策略库的 ReAct 实现，可独立调用或被其他策略组合：
  ReActAgent._strategy_cycle() → ReActStrategy.execute()
  PlannerStrategy 执行阶段 / ReflectionStrategy 收集阶段 → ReActStrategy.execute()

依赖方向：本模块只依赖 ports + shared + 标准库，不 import agent/；策略持有本次 execute
的 outcome、计数和工具事实，并通过 reasoning 值对象接收运行参数，可独立测试和组合复用。

事件流输出设计（与 executor.py 原 ReActAgent 一致）：
    LLM 原始流 → type=reasoning（逐 token）
               → type=message（逐 token）
    发现 tool_calls → type=tool_call
    执行工具       → type=tool_result
    LLM 下一轮原始流 → type=reasoning / message
    完成           → type=done
LLM 通道双形态（execute stream_mode 参数）：
    True（默认）→ async_generate 流式，reasoning/message 逐 token 事件
    False        → generate() 非流式一次拿 StreamResult，reasoning/message 整条一次性
                   （SSE 协议同构；后台子 Agent 无人订阅场景，Phase C）

每次 execute() 是独立的：结果写入 self.outcome，调用方（ReActAgent）读取后组装 AgentResult。
"""

import asyncio
import copy
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway, StreamResult
from app.domain.ports.tool_execution import (
    ToolCallContext,
    ToolCleanupState,
    ToolEffectState,
    ToolExecutionState,
    ToolFact,
)
from app.domain.ports.tool_gateway import ErrorCode, ToolGateway, ToolResult
from app.domain.reasoning.tool_batch import DEFAULT_BATCH_CLEANUP_GRACE, ToolBatchCollector, ToolBatchRunner
from app.shared.error_handling import (
    AgentErrorAction,
    AgentErrorKind,
    AgentRunError,
    ErrorHandlerRegistry,
)
from app.shared.events import (
    build_done_event,
    build_error_event,
    build_info_event,
    build_message_event,
    build_reasoning_event,
    build_tool_call_event,
    build_tool_result_event,
)
from app.shared.exceptions import (
    AppError,
    ContextWindowExceededError,
    LLMCancelledError,
    LLMDeadlineExceededError,
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)
from app.shared.json_schema import create_schema_validator
from app.shared.observation import isolate_observation

from ._common import (
    GuardResult,
    dispatch_error,
    evaluate_guard,
    merge_usage,
    reject_concurrent_runs,
)
from ._react_protocol import (
    _FINAL_ANSWER_TOOL,
    action_fingerprint,
    build_final_answer_tool,
    extract_final_answer,
    has_final_answer,
    tool_call_identity_error,
    tool_call_name,
)
from .execution import (
    ContextWindowLimits,
    ExecutionLimits,
    ModelOptions,
    ReasoningRunScope,
    RecoveryBudget,
    ToolExecutionOptions,
)

# 策略层标准库日志（对齐「只依赖 ports + shared + 标准库」依赖方向，不用 platform 的
# get_logger）；logger 名对齐 app.* 命名空间，可被 setup_logging 的 handler 捕获。
_logger = logging.getLogger("app.domain.reasoning.react")

# 工具结果回喂截断标记：截断时追加，模型可知结果不完整（而非误以为完整）
_TRUNCATED_MARKER = "\n[结果已截断]"

# max_execution_time 是 ReAct 业务循环的取消触发点。内部 LLM deadline 提前预留一小段
# 时间用于 close / settle / 日志等终止清理，避免与外层 asyncio.timeout 同刻二次取消。
# timeout scope 之外的领域终态组装不再发起 LLM/工具副作用，但可扩展 handler 可能
# 形成额外尾部延迟；asyncio 不对同步阻塞或吞取消代码作绝对返回时限保证。
_MAX_EXECUTION_CLEANUP_GRACE = 1.0
_EXECUTION_CLEANUP_GRACE_RATIO = 0.1

# execute_tool_calls 的默认工具执行选项：timeout / max_attempts 均为 None，交由执行器按
# 工具自声明或全局配置解析（语义同 execute() 的 None）。用模块级常量而非在参数默认值里
# 调用构造器，避免 B008（函数调用出现在默认值中）触发新增 lint 债务。
_DEFAULT_TOOL_EXECUTION = ToolExecutionOptions()


def _resolve_deadlines(max_execution_time: float | None, now: float) -> tuple[float | None, float | None]:
    """由总执行时限派生 (内部 LLM deadline, 外层硬超时)；未设限时两者均为 None。

    两个值都以同一 `now`（monotonic 秒）为基准：内部 deadline 提前一个有界收尾窗口
    （`min(_MAX_EXECUTION_CLEANUP_GRACE, 总时长 × _EXECUTION_CLEANUP_GRACE_RATIO)`），
    使 close / settle / 日志有机会在外层 timeout 取消 task 前完成收尾。负时限按 0 处理。

    `now` 由调用方传入以保持纯函数可测；消费者分别按 `time.monotonic()`（护栏与 LLM
    期限）和 `loop.time()`（`asyncio.timeout_at`）比较——默认事件循环两者同源，自定义
    事件循环需保持同源，否则收尾窗口的相对关系不再成立。
    """
    if max_execution_time is None:
        return None, None
    duration = max(0.0, max_execution_time)
    cleanup_grace = min(_MAX_EXECUTION_CLEANUP_GRACE, duration * _EXECUTION_CLEANUP_GRACE_RATIO)
    hard_timeout_at = now + duration
    return hard_timeout_at - cleanup_grace, hard_timeout_at


def _terminal_result(
    current_result: StreamResult | None,
    last_visible_result: StreamResult | None,
) -> StreamResult | None:
    """终止时选择最新可用成果：当前轮有可见进度则优先，否则保留上一完成轮。"""
    if current_result is not None and (current_result.content.strip() or current_result.reasoning_content.strip()):
        return current_result
    return last_visible_result


def _unaccounted_usage(
    current_result: StreamResult | None,
    exception_usage: dict | None = None,
) -> dict | None:
    """返回当前未归账用量；异常携带值优先，避免与 result 中的同一笔 usage 双计。"""
    if exception_usage is not None:
        return exception_usage
    return current_result.usage if current_result is not None else None


def _truncate_with_marker(text: str, limit: int) -> str:
    """截断到 limit 字符；截断时追加截断标记（预留标记长度，总长不超 limit）。"""
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATED_MARKER)] + _TRUNCATED_MARKER


def _build_assistant_message(stream_result: StreamResult) -> dict | None:
    """组装本轮 assistant 消息（纯转换）；纯空轮返回 None（不写历史）。

    - 纯空轮（content / reasoning / tool_calls / has_reasoning 全无）不组装：空 assistant
    消息无信息量，空输出重试累积会污染上下文。
    - has_reasoning 留在判定内——thinking 模型返回空 reasoning 也回喂该字段（防 400）；
    - tool_calls 必须保留在与后续 tool 回执配对的 assistant 消息上（否则下一轮 400）。
    - 是否写入 messages 由调用方决定（见 execute 第 8 步）。
    """
    if not (
        stream_result.content
        or stream_result.reasoning_content
        or stream_result.has_reasoning
        or stream_result.tool_calls
    ):
        return None
    message: dict = {"role": "assistant", "content": stream_result.content}
    if stream_result.reasoning_content or stream_result.has_reasoning:
        message["reasoning_content"] = stream_result.reasoning_content
    if stream_result.tool_calls:
        message["tool_calls"] = stream_result.tool_calls
    return message


def _tool_protocol_error_detail(
    finish_reason: str,
    tool_calls: list[dict],
    *,
    has_tools: bool,
    output_schema: dict | None,
) -> str | None:
    """本轮工具调用响应无效时返回协议异常原因；有效返回 None（纯判定，不改状态）。

    必须在写入 assistant 历史前拦截：留下的 tool_calls 无法与 tool 回执配对（身份不可用
    与「没有 tool_calls」等价，同样无法配对），下一轮请求会被 OpenAI 兼容网关以 400 拒绝。
    四类异常按固定优先级取首个命中——结构缺陷（无调用 / 无可用工具 / 身份不可用）优先于
    用法缺陷（终止工具与普通工具混用），保证归因指向更根本的一侧。
    """
    if finish_reason != "tool_calls":
        return None
    if not tool_calls:
        return "finish_reason=tool_calls 但未返回工具调用（协议异常）"
    if not has_tools:
        return "模型返回 tool_calls 但当前无可用工具（协议异常）"
    identity_error = tool_call_identity_error(tool_calls)
    if identity_error is not None:
        return identity_error
    if len(tool_calls) > 1 and has_final_answer(tool_calls, output_schema):
        return "final_answer 不能与其他工具同轮调用（协议异常）"
    return None


def _aborted_call_outcome(
    tool_call: dict,
    call_facts: list[ToolFact],
) -> tuple[ToolResult, dict, float]:
    """工具未正常返回（取消 / 超时 / 停跑 / 仍有任务挂起）时，由已接管事实还原可回执结局。

    权威事实按「在途 attempt 事实 > 已带结果的操作事实 > 最后一个 attempt 事实 > 操作事实」
    选取：预登记的 NOT_STARTED 不得盖过已完成的 attempt 快照（旧 / 外部 Gateway 可能只发布
    attempt 事实）；还有 attempt 停在 RUNNING 时不得采信操作事实里的旧结果。
    操作事实的归属是显式的：带结果的优先，同为带结果时取最后插入的一条；启动前那条无结果的
    预登记只用于「从未启动」的结论，不参与终局判定。

    参数解析失败按空参回执且不再次调用工具（工具本就没执行，报未执行比报参数错误更准）。
    返回 (结果, 回执参数, 耗时)。
    """
    # ① 先尽力解析出「模型原本想传的参数」。它只用于回执展示：工具根本没执行，
    #    所以解析失败就到此为止，不再补一条参数错误——那会把「没执行」这个主因盖掉。
    tool_args: dict = {}
    try:
        parsed_args = json.loads(tool_call["function"]["arguments"])
        if isinstance(parsed_args, dict):
            tool_args = parsed_args
    except KeyError, TypeError, json.JSONDecodeError:
        pass  # 缺 arguments 字段 / 值不是字符串 / 不是合法 JSON，一律按「参数不可用」处理

    # ② 在事实里挑出权威的那一条。一个 call 最多两类事实：
    #    操作事实（attempt_id 为空，启动前预登记）+ 每次真实尝试的 attempt 事实（attempt_id 非空）。
    #    操作事实可能不止一条：业务键复用时真实事实引用原规范操作，而 call 持自己的新
    #    operation_id，同一 call 因此出现两条 attempt_id 为空的事实。归属显式规定为「带结果的
    #    优先、同为带结果取最后插入」，不依赖「先插入的是哪条」——预登记那条只用于证明未启动。
    operation_facts = [fact for fact in call_facts if fact.attempt_id is None]
    attempt_facts = [fact for fact in call_facts if fact.attempt_id is not None]
    operation_fact = next(
        (fact for fact in reversed(operation_facts) if fact.result is not None),
        operation_facts[-1] if operation_facts else None,
    )
    # 还有 attempt 停在 RUNNING 时这个 call 没有终局，优先按「结果尚未确认」回执：
    # 操作事实在每次 attempt 完成时都会被重发（revision=attempt+1），在途重试期间它带的是
    # 上一次尝试的旧结果，直接采信会把「未确认」报成「已确认失败」。
    in_flight = next(
        (
            fact
            for fact in reversed(attempt_facts)
            if fact.result is None and fact.execution_state == ToolExecutionState.RUNNING
        ),
        None,
    )
    if in_flight is not None:
        fact = in_flight
    # 没有在途 attempt 时，操作事实已经带结果 → 这是一个已确认的终局，直接采信；
    # 启动前那条预登记的 NOT_STARTED 结果为空，因此不会误入这一支。
    elif operation_fact is not None and operation_fact.result is not None:
        fact = operation_fact
    else:
        # 否则退到最后一个 attempt 快照（列表按收集顺序追加，故最后一条 = 最近一次尝试）；
        # 连 attempt 事实都没有时，才退回操作事实本身（可能只是那条预登记的 NOT_STARTED）。
        fact = attempt_facts[-1] if attempt_facts else operation_fact

    # ③ 由选中的事实派生回执。耗时只在事实真的带结果时才有值，否则记 0，不把 None 传下去。
    elapsed = (fact.result.execution_time or 0.0) if fact is not None and fact.result is not None else 0.0
    if fact is not None and fact.result is not None:
        # 事实链里已有确认结果：原样回执（工具其实跑完了，只是返回值没交回本批次）。
        exec_result = fact.result
    elif fact is None or fact.execution_state == ToolExecutionState.NOT_STARTED:
        # 一条事实都没有，或只有启动前那条 NOT_STARTED：工具从未真正开始，报「未执行」。
        # 既然从未启动，副作用必然是 NONE，可以写死。
        exec_result = ToolResult(False, "", error="工具调用未执行（运行已终止）", effect_state=ToolEffectState.NONE)
    else:
        # 有事实但确认不了结果（典型是 RUNNING）：报「结果尚未确认」。
        # effect_state 沿用事实里的值——副作用到底发生了没有只有事实知道，这里不能假设成 NONE。
        exec_result = ToolResult(False, "", error="工具执行结果尚未确认", effect_state=fact.effect_state)
    return exec_result, tool_args, elapsed


def _group_failures_by_kind(failures: list[dict]) -> dict[AgentErrorKind, list[dict]]:
    """按错误类别聚类本轮失败的工具记录（「记录 → kind」映射的唯一出处）。

    参数 JSON 解析失败属协议类（PARSE_FAILED，另受协议修正预算约束），其余业务失败归
    TOOL_FAILED；分发方据此按固定顺序聚合原因并仲裁。
    """
    grouped: dict[AgentErrorKind, list[dict]] = {}
    for record in failures:
        kind = (
            AgentErrorKind.PARSE_FAILED
            if record.get("error_code") == ErrorCode.JSON_PARSE.value
            else AgentErrorKind.TOOL_FAILED
        )
        grouped.setdefault(kind, []).append(record)
    return grouped


@dataclass
class ReActOutcome:
    """ReAct 策略执行的最终结果载体（供桥接方组装 AgentResult）。"""

    content: str = ""
    reasoning: str = ""
    structured: dict | None = None  # final_answer 结构化最终答案（output_schema 启用时）
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    total_tokens: int = 0
    usage: dict | None = None
    error: str | None = None
    success: bool = False


class ReActStrategy:
    """
    ReAct 领域推理流程；持有单次 execute 的结果，不持有 Agent 生命周期状态。

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

        # 连续空输出重试计数（execute 每次开头重置；本轮有产出清零、空输出 +1）
        self._empty_retries = 0
        # 连续 LLM 失败重试计数（execute 每次开头重置；成功轮清零、失败轮 +1）
        self._llm_fail_retries = 0
        # 连续工具调用协议异常计数（execute 开头重置；合法工具协议轮清零）。
        self._tool_protocol_retries = 0
        # 循环停滞检测状态：上一轮动作指纹 + 连续相同计数（execute 开头重置）
        self._last_action_fp: str | None = None
        self._stall_count = 0
        # 结果载体，execute() 结束后读取
        self.outcome: ReActOutcome | None = None
        self._tool_call_records: list[dict[str, Any]] = []
        self._tool_facts: list[ToolFact] = []

    @property
    def tool_facts(self) -> tuple[ToolFact, ...]:
        """返回本次运行已接管的工具事实快照。"""
        return tuple(copy.deepcopy(fact) for fact in self._tool_facts)

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
        output_schema: dict | None = None,
        stream_mode: bool = True,
        baseline_usage: dict | None = None,
    ) -> AsyncGenerator[str]:
        """
        ReAct 主循环。

        循环流程（各分支经错误处理分发，默认行为 = 现有逻辑）：
            1. 调用前护栏（cancel > deadline > cost）→ 类型化终态，不开始新轮
            2. 上下文预算裁剪（所有继续路径共用，保证每次 LLM 调用前消息有界）
            3. LLM 推理（流式/非流式均透传 cancel_event 与绝对 deadline）
            4. 接管本轮成果和 usage 后复查 cancel > deadline > cost
            5. LLM 失败（stream_result.error 非空）→ LLM_FAILED 分发
               （默认 STOP 短路；handler 可 CONTINUE 重试，重试受 max_llm_fail_retries 上限硬终止）
            6. 模型拒答（refusal 字段 / content_filter）→ REFUSED 分发（默认停机）
            7. 检查工具协议信号；无效响应先短路，不写入消息历史
               （协议异常受 max_tool_protocol_retries 独立修正上限约束）
            8. 追加有效 assistant 消息（纯空轮不追加）
            9. 根据 finish_reason 决定下一步
               - "tool_calls" → final_answer 检测 → 停滞检测（连续相同超限硬终止）→ 执行工具，追加结果，继续循环
               - "stop"       → 生成最终结果，结束循环
               - "length"     → 生成部分结果，结束循环
               - 空输出       → 连续计数 +1；超过 max_empty_retries 硬终止，否则错误分发重试
            10. 循环耗尽 → MAX_TURNS 分发（默认兜底）
            11. 各类异常处理

        实现：主循环仅保留骨架，各终止/错误分支拆分为职责单一的方法
        （_finalize_* / _handle_*；护栏求值与出口收敛为 execute 内的 `_guard_exit`
        闭包，轮询型与异常出口共用同一口径），以 `outcome is not None` 作为终止信号。

        Args:
            user_input: 用户原始输入（保留兼容，循环内部以 messages 为准）
            messages: 可修改的消息列表副本
            run: 本次运行身份、完成信号和取消传播链；取消信号同时传给流式与
                非流式 LLM 调用，并参与各护栏检查
            model: LLM 采样温度与单轮最大输出 token
            limits: 最大迭代轮数、总执行墙钟和连续相同动作上限；墙钟到期后的
                领域终态组装不再发起 LLM 或工具副作用
            context_window: 最近消息轮次与消息 token 上限；None 表示对应维度不裁剪
            recovery: 空输出、LLM 失败和工具协议异常的连续恢复预算；N 表示最多
                恢复 N 次，第 N+1 次仍失败则硬终止，0 表示首次失败即终止
            tool_execution: 工具执行超时和最大执行次数；None 时采用工具执行器配置
            output_schema: 最终答案结构化 JSON Schema（None=不启用），固定 2020-12。
                定义非法时在模型调用前抛 SchemaError；启用时注入
                final_answer 工具，模型最后调用提交结构化结果并终止循环
            stream_mode: LLM 通道——True=流式 async_generate（默认，逐 token 事件，
                面向 chat SSE 订阅者）；False=非流式 generate()（一次拿完整 StreamResult，
                后台子 Agent 无人订阅场景，Phase C）。主循环护栏语义（成本/失败/拒答/
                工具/停滞/空输出）两通道一致；差异：False 下 reasoning/message 事件为
                整条一次性，并在 LLM 失败时补 error 事件
            baseline_usage: 跨阶段复用方注入的累计用量基线（dict，None/空=不启用）——
                本 execute 开始前已累计的 token 用量（如 planner 步骤子跑：已完成步骤
                react + plan/replan 结构化用量）。仅参与成本判定（成本检查 = 基线 +
                本轮局部累计），**不进本 execute 报告口径**（outcome.usage / total_tokens
                恒为本次子跑局部——调用方各归并一次，防双计）。None / 空 = 单跑
                （ReActAgent / reflection 首次收集）等价现状零开销。

        Yields:
            SSE 事件字符串（reasoning / message / tool_call / tool_result / info / done）；
            流式下 reasoning/message 逐 token，非流式下为整条一次性（协议同构）

        Raises:
            AgentRunError: 错误处理器返回 RAISE——已完成分类的领域错误，直接传播给
                BaseAgent.run，不再被下方兜底改写为 UNKNOWN
            ToolCancelledError / ToolDeadlineExceededError / ToolRunStoppedError: 批次已接管
                兄弟成果与完整协议回执后，运行级控制原因继续类型化上抛，由上层选择终态；
                不折算为可重试工具失败或一次正常 done
        """

        # 防 handler CONTINUE 无限重试烧钱：连续空输出 / LLM 失败 / 循环停滞计数，超过上限硬终止
        # 连续空输出重试计数：execute 每次独立（有产出清零 / 空输出 +1，见主循环）
        self._empty_retries = 0
        # LLM 失败重试计数：execute 每次独立（成功轮清零 / 失败轮 +1，见主循环）
        self._llm_fail_retries = 0
        # 工具协议修正计数：三类协议异常共享，合法工具协议轮才清零。
        self._tool_protocol_retries = 0
        # 循环停滞检测：execute 每次独立（相同动作指纹 + 连续计数，见 _bump_stall）
        self._last_action_fp = None
        self._stall_count = 0
        self.outcome = None
        self._tool_call_records = []
        self._tool_facts = []

        tool_defs = self._tools.get_openai_tools() if self._tools else None
        # 结构化最终答案：注入 final_answer 工具（模型最后调用提交结构化结果并终止）
        if output_schema is not None:
            create_schema_validator(output_schema)
            tool_defs = [*(tool_defs or []), build_final_answer_tool(output_schema)]
        has_tools = bool(tool_defs)

        # 最近一轮已归账且具有用户可见成果的完整响应。仅 content/reasoning
        # 可更新它；未执行 tool_calls、usage 和 finish_reason 不构成可见成果。
        last_visible_result: StreamResult | None = None
        # 本轮正在生成、尚未完成正常归账的 LLM 输出，可能为半成品。
        current_result: StreamResult | None = None

        total_usage: dict = {}

        # 记录进入 timeout 的 task：超时降级时判别「真超时」与「生成器被 finalizer
        # 关闭」（慢消费者场景 aclose 由不同 task 驱动，需干净停止不 yield 降级事件）。
        entered_task = asyncio.current_task()

        # LLM-044：内部执行截止与外层硬超时由同一处派生（见 _resolve_deadlines）。内部
        # deadline 供流式 / 非流式 LLM 调用按同一期限受控（集成 reserve/create/整流读取期
        # 执行控制），并早于外层 timeout 取消触发点一个有界窗口，使 close/settle/日志
        # 有机会先完成收尾。
        deadline, hard_timeout_at = _resolve_deadlines(
            limits.max_execution_time,
            asyncio.get_running_loop().time(),
        )

        hard_timeout_scope: asyncio.Timeout | None = None
        # 轮次由下方 for 绑定，循环开始前不存在；异常出口也要读它，
        # 预置 0（尚无完成轮次）避免处理器 UnboundLocalError 掩盖原始异常。
        iteration = 0

        # 护栏统一出口：四处调用点（调用前 / 归账后轮询，超时与 LLM 终结信号的异常出口）
        # 共用同一次求值——CANCELLED > TIMEOUT > COST_EXCEEDED > CONTEXT_EXCEEDED 的优先级
        # 与成本口径（baseline + 本轮局部累计）不允许在任一出口漂移。
        # 依赖不变量：捕获的 total_usage 只做 .update() 不重绑定，baseline_usage / deadline /
        # run / limits 定义后不再变化；iteration 由 for 重新绑定，闭包按调用时读取当前轮。
        # 命中护栏即写 self.outcome（与各 _finalize_* 一致），调用方据此终止。
        async def _guard_exit(
            *,
            result: StreamResult | None,
            cancelled: bool = False,
            deadline_exceeded: bool = False,
            context_error: ContextWindowExceededError | None = None,
        ) -> list[str]:
            guard = evaluate_guard(
                cancel_event=run.cancel_event,
                deadline=deadline,
                cost_limiter=self._cost_limiter,
                running_usage=merge_usage(baseline_usage, total_usage),
                cancelled=cancelled,
                deadline_exceeded=deadline_exceeded,
                context_error=context_error,
            )
            if guard is None:
                # 异常出口必带硬信号，evaluate_guard 对任一信号短路返回，不可能为空；
                # 断言守住调用契约，防止将来新增出口漏传信号后静默不置终态。
                assert not (cancelled or deadline_exceeded or context_error is not None), (
                    "异常出口必须携带至少一个终止信号，否则不会产生终态"
                )
                return []
            return await self._finalize_guard_result(
                guard,
                result,
                iteration,
                total_usage,
                limits.max_execution_time,
            )

        try:
            async with asyncio.timeout_at(hard_timeout_at) as hard_timeout_scope:
                for iteration in range(1, limits.max_iterations + 1):
                    # ----- 1. 每次付费调用前统一执行护栏：包括用户取消、执行超时以及成本超限 -----
                    for e in await _guard_exit(result=last_visible_result):
                        yield e
                    if self.outcome is not None:
                        return

                    yield build_info_event(f"第 {iteration} 轮推理")

                    # ----- 2. 上下文预算：模型本次调用前作为 gatekeeper 裁剪 -----
                    # 置于循环顶部（而非工具路径后）：所有继续路径共用——工具回喂、
                    # LLM 失败重试 / final_answer 回喂重试 / 空输出重试，下一次 LLM
                    # 调用前均裁剪，否则非工具路径上下文无限增长、预算失效。
                    if self._context_budget is not None:
                        self._context_budget.trim_messages(
                            messages,
                            max_rounds=context_window.max_rounds,
                            max_tokens=context_window.max_tokens,
                        )

                    # ----- 3. LLM 推理 -----
                    # 双通道（stream_mode）：流式 async_generate 逐 token 事件（默认，
                    # chat SSE 订阅者）；非流式 generate() 一次拿完整 StreamResult（后台
                    # 子 Agent 无人订阅，Phase C）。两者最终填同一 stream_result → 下游
                    # 分支逻辑全复用（对齐 OpenAI run()/run_streamed() 同一 agent loop）。
                    stream_result = StreamResult()
                    # 取消/deadline/硬超时可能在 async_generate 中途逸出，须由
                    # current_result 保留部分 content/reasoning。
                    current_result = stream_result
                    if stream_mode:
                        async for event in self._llm.async_generate(
                            messages=messages,
                            tools=tool_defs,
                            temperature=model.temperature,
                            max_tokens=model.max_tokens,
                            result=stream_result,
                            cancel_event=run.cancel_event,
                            deadline=deadline,
                        ):
                            yield event
                    else:
                        async for event in self._llm_round_non_streaming(
                            stream_result,
                            messages,
                            tool_defs,
                            model.temperature,
                            model.max_tokens,
                            cancel_event=run.cancel_event,
                            deadline=deadline,
                        ):
                            yield event

                    # 累计 token 用量
                    if stream_result.usage:
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            total_usage[k] = total_usage.get(k, 0) + stream_result.usage.get(k, 0)

                    # 只保存最近一轮可见成果的完整快照。当前轮仅有尚未执行的
                    # tool_calls 或完全为空时，不能覆盖更早的 content/reasoning。
                    if stream_result.content.strip() or stream_result.reasoning_content.strip():
                        last_visible_result = stream_result
                    # 本轮正常返回并归账后立即清空 current_result，防止 usage 双计。
                    current_result = None

                    # ----- 4. 成功返回并归账后统一复查 -----
                    # 取消、期限和成本可能在 await 期间发生；先吸收本轮成果与 usage，
                    # 再按固定优先级收尾，且不允许继续工具副作用或下一次付费调用。
                    for e in await _guard_exit(result=last_visible_result):
                        yield e
                    if self.outcome is not None:
                        return

                    # ----- 5. LLM 失败（stream_result.error 非空）→ LLM_FAILED 分发 -----
                    # 短路返回失败结果，不把「失败」当「空输出」继续空转重试（浪费 LLM 调用 + 错误信息不准确）。
                    # 正常空回（stop + 空 content）error 为 None，仍走下方「空输出重试」逻辑。
                    if stream_result.error:
                        # 连续 LLM 失败重试计数（对齐空输出护栏）：失败轮 +1，
                        # 成功轮清零（见下方）。取消不计数（上方已 return）。
                        self._llm_fail_retries += 1
                        # LLM 调用失败 → 错误分发（默认 STOP 短路；handler 可重试/上抛，
                        # 重试受 max_llm_fail_retries 上限硬终止）
                        for e in await self._handle_llm_failed(
                            stream_result,
                            iteration,
                            total_usage,
                            recovery.max_llm_fail_retries,
                        ):
                            yield e
                        if self.outcome is not None:
                            return
                        continue  # CONTINUE：重试
                    # LLM 成功轮（error 为 None）→ 失败重试计数清零（对齐空输出「有产出清零」）
                    self._llm_fail_retries = 0

                    # ----- 6. 模型拒答（refusal 字段 / content_filter）→ REFUSED 分发（默认 STOP）-----
                    # 显式拒答信号（LLM-004 原则：拒答基于显式信号，不靠 content 空推断）；
                    # 拒答终止不误判为成功答案、不空转重试。DeepSeek stop+空 content 属
                    # 空回答（非显式拒答），保持现有正常结束语义。
                    if stream_result.refusal or stream_result.finish_reason == "content_filter":
                        for e in await self._finalize_refused(stream_result, iteration, total_usage):
                            yield e
                        return

                    full_reasoning = stream_result.reasoning_content
                    full_content = stream_result.content
                    finish_reason = stream_result.finish_reason or ""

                    # ----- 7. 工具调用协议异常：finish_reason=tool_calls 但响应无效 -----
                    # 工具调用信号与数据 / 能力 / 身份不一致时，整条 assistant 响应无效；
                    # 必须先校验再写历史，否则会留下无法配对的 tool_calls（下一轮 400）。
                    # 四类判据与优先级见 _tool_protocol_error_detail。
                    detail = _tool_protocol_error_detail(
                        finish_reason,
                        stream_result.tool_calls,
                        has_tools=has_tools,
                        output_schema=output_schema,
                    )
                    if detail is not None:
                        for e in await self._handle_tool_protocol_error(
                            detail,
                            full_reasoning,
                            iteration,
                            total_usage,
                            recovery.max_tool_protocol_retries,
                        ):
                            yield e
                        if self.outcome is not None:
                            return
                        continue

                    # ----- 8. 将 LLM 回复追加到消息历史 -----
                    # 字段组装规则见 _build_assistant_message（纯转换）；是否提交由本步决定。
                    assistant_msg = _build_assistant_message(stream_result)
                    if assistant_msg is not None and (
                        finish_reason != "tool_calls" or has_final_answer(stream_result.tool_calls, output_schema)
                    ):
                        # 若本次是普通工具批次调用，则此处不提交 assistant + tool 历史。
                        # 因为这里若先写 assistant.tool_calls，消费者在下一条 SSE 后关闭生成器，
                        # 就会留下缺少 tool 回执的本地非法历史，即 tool_calls 和 tool_call_records 不匹配
                        messages.append(assistant_msg)

                    # ----- 9. 根据 finish_reason 决定下一步 -----
                    if finish_reason == "tool_calls":
                        # （1）协议正常的工具调用 → 执行或 final_answer 修正
                        self._empty_retries = 0
                        yield build_info_event(f"检测到 {len(stream_result.tool_calls)} 个工具调用")

                        # Final Answer 工具：结构化最终答案 → 提取终止 / 回喂继续
                        # （注入工具非注册工具，识别经 _react_protocol.has_final_answer，
                        #   分支与提取仍在主循环，不进 execute_tool_calls）
                        if has_final_answer(stream_result.tool_calls, output_schema):
                            # 判定为真即已启用；断言只为收窄类型（schema 供提取校验用）
                            assert output_schema is not None
                            for e in await self._handle_final_answer(
                                stream_result.tool_calls,
                                messages,
                                iteration,
                                output_schema,
                                total_usage,
                                full_reasoning,
                                recovery.max_tool_protocol_retries,
                            ):
                                yield e
                            if self.outcome is not None:
                                return
                            continue  # final_answer CONTINUE：回喂后继续

                        # 循环停滞检测：相同工具 + 参数连续重复 → STALLED 分发硬终止
                        if self._bump_stall(stream_result.tool_calls) > limits.max_same_action_turns:
                            for e in await self._finalize_stalled(
                                stream_result.tool_calls,
                                iteration,
                                total_usage,
                                full_reasoning,
                                self._stall_count,
                            ):
                                yield e
                            return

                        async for event in self._handle_tool_calls(
                            stream_result.tool_calls,
                            messages,
                            iteration,
                            total_usage,
                            full_reasoning,
                            tool_execution,
                            recovery.max_tool_protocol_retries,
                            run=run,
                            deadline=deadline,
                            cleanup_deadline=hard_timeout_at,
                            batch_cleanup_grace=limits.batch_cleanup_grace,
                            assistant_message=assistant_msg,
                        ):
                            yield event
                        if self.outcome is not None:
                            return
                        continue
                    elif finish_reason in ("stop", "length") or full_content.strip():
                        # （2）stop / length / 有内容 → 正常结束
                        self._empty_retries = 0  # 本轮有产出（stop / length / 有内容）→ 空输出连续计数清零
                        for e in self._finalize_outcome(
                            success=bool(full_content.strip()),
                            content=full_content.strip(),
                            reasoning=full_reasoning.strip(),
                            iteration=iteration,
                            total_usage=total_usage,
                        ):
                            yield e
                        return
                    else:
                        # （3）空输出 → 错误分发（默认 CONTINUE 重试；handler 可终止/上抛）
                        self._empty_retries += 1  # 本轮空输出 → 空输出连续计数 +1
                        for e in await self._handle_empty_output(
                            full_reasoning,
                            iteration,
                            total_usage,
                            recovery.max_empty_retries,
                        ):
                            yield e
                        if self.outcome is not None:
                            return
                        # CONTINUE（默认）：重试（_handle_empty_output 已产出重试信息）

                # ----- 10. 达到最大迭代次数 → 错误分发（默认 STOP 兜底；handler 可上抛） -----
                for e in await self._finalize_max_turns(last_visible_result, limits.max_iterations, total_usage):
                    yield e
        # ----- 11. 异常处理 -----
        except AgentRunError:
            # 异常来源：本方法内各 `_finalize_*` / `_handle_*` 经 `_dispatch` →
            # `dispatch_error` 调用错误处理器；处理器返回 RAISE 时，后者构造并抛出
            # AgentRunError。它是已经完成分类的领域错误，直接传播给 BaseAgent.run，
            # 不能再被下方 Exception 兜底改写为 UNKNOWN。
            raise
        except ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError:
            # 批次已接管兄弟成果及完整协议回执；运行级控制原因继续类型化上抛。
            # 上层按自身运行契约选择终态，不把取消/期限降为可重试工具失败。
            raise
        except TimeoutError as exc:
            # TimeoutError 异常来源有两类：
            # ① 本方法的 `asyncio.timeout_at` 到期，取消当前 task，
            # scope 退出时将自身触发的 CancelledError 转为内置 TimeoutError；
            # ② LLMGateway 及其流读取/close/settle/log 等内部组件，或其他被调用端口，
            # 直接抛出并穿透的普通 TimeoutError。

            # 关闭判别：生成器正被 finalizer/aclose 关闭（不同 task 驱动）或外部取消
            # → 干净停止，不 yield 降级事件（避免 RuntimeError: async generator ignored GeneratorExit）
            cur = asyncio.current_task()
            if cur is None or cur is not entered_task or cur.cancelling() > 0:
                return

            terminal_result = self._take_over_failure(current_result, last_visible_result, total_usage)
            # 只有本次 asyncio.timeout_at 确实到期，才能解释为 ReAct 总执行超时；仅比较当前
            # 时钟与 deadline 会把「期限已过但 timeout 回调尚未取消 task」误判为硬超时。
            if hard_timeout_scope is None or not hard_timeout_scope.expired():
                # 若硬超时范围为空或硬超时未到期则说明这个 TimeoutError 与外层 timeout无关
                for event in await self._finalize_unknown(terminal_result, iteration, total_usage, exc):
                    yield event
                return
            else:
                # 外层 timeout 真实到期 → 保留当前轮已生成部分成果；若当前轮尚无可见进度则沿用上一轮。
                for event in await _guard_exit(result=terminal_result, deadline_exceeded=True):
                    yield event
                return
        except (
            ContextWindowExceededError,
            LLMCancelledError,
            LLMDeadlineExceededError,
        ) as exc:
            # 三类异常均是 LLM 边界已经识别的终结信号：统一接管当前成果与
            # 未归账 usage，再由共享 guard 处理并发信号优先级和终态类型。
            exception_usage = getattr(exc, "usage", None)
            terminal_result = self._take_over_failure(current_result, last_visible_result, total_usage, exception_usage)
            for event in await _guard_exit(
                result=terminal_result,
                cancelled=isinstance(exc, LLMCancelledError),
                deadline_exceeded=isinstance(exc, LLMDeadlineExceededError),
                context_error=(exc if isinstance(exc, ContextWindowExceededError) else None),
            ):
                yield event
            return
        except Exception as e:  # noqa: BLE001
            # 异常来源：try 范围内 ContextBudgetPort 裁剪、LLMGateway 调用、工具处理、
            # 消息/结果组装及其他策略内部代码抛出的、未被前面专用分支分类的 Exception。
            #   - AgentRunError、内置 TimeoutError 和三类 LLM 终结信号已被前置分支接管；
            #   - asyncio.CancelledError / GeneratorExit 属于 BaseException，不会在此捕获。

            # 关闭判别（对齐 TimeoutError 分支）：生成器被 finalizer/aclose 关闭
            # 或外部取消 → 干净停止，不 yield 降级事件
            cur = asyncio.current_task()
            if cur is None or cur is not entered_task or cur.cancelling() > 0:
                return

            # 真异常 → UNKNOWN 分发（默认 STOP，优先保留当前轮部分进度 + 证据链）。
            terminal_result = self._take_over_failure(current_result, last_visible_result, total_usage)
            for ev in await self._finalize_unknown(terminal_result, iteration, total_usage, e):
                yield ev
            return

    async def _llm_round_non_streaming(
        self,
        stream_result: StreamResult,
        messages: list[dict],
        tool_defs: list[dict] | None,
        temperature: float,
        max_tokens: int,
        *,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> AsyncGenerator[str]:
        """非流式单轮 LLM：generate() 一次拿完整 StreamResult，合成整条 SSE 事件。

        契约映射（对齐流式整流器的可观测语义，工业实证见 ADR stream-channel）：
        - generate() 返回 None（可恢复错误重试耗尽）→ 等价整流器「放弃」：置
          stream_result.error + error 事件 → 主循环 LLM_FAILED 分发（不误判空输出）。
        - generate() 抛 AppError（共享 AppError 树：LLMAPIError 4xx/认证/校验 与熔断
          CircuitBreakerOpenError 均在内）→ 等价整流器 create 失败：同样折算
          LLM_FAILED（流式路径此情形从不走 UNKNOWN；熔断被本分支捕获 → 两通道一致归 LLM_FAILED）。
        - AppError 树外异常（编程错误）不在此吞 → 冒泡外层 except → UNKNOWN。
        - 成功 → 字段就地填回 stream_result + 整条 reasoning（先）→ message（后）
          事件（协议与 async_generate 逐 chunk 一致；空串不产事件对齐整流）。
        """
        try:
            result = await self._llm.generate(
                messages=messages,
                tools=tool_defs,
                temperature=temperature,
                max_tokens=max_tokens,
                model_key="main",  # 显式 main 对齐 async_generate 默认，勿用 generate 默认 "fast"
                # LLM-044：透传执行控制——generate 内部 reserve/create/返回前检查点
                # 按同一 cancel_event + 绝对 deadline 受控（非流式不再仅靠轮末补查）。
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except LLMCancelledError, LLMDeadlineExceededError, ContextWindowExceededError:
            # 执行终止（用户取消 / 整体期限 / 上下文预算超限）→ 交由主循环映射
            # CANCELLED/TIMEOUT/CONTEXT_EXCEEDED，不折算 LLM_FAILED。
            raise
        except AppError as e:
            exc_text = str(e)[:500]  # 对齐整流器错误截断上限
            stream_result.error = exc_text
            yield build_error_event(f"LLM 调用失败: {exc_text}")
            return

        if result is None:
            msg = "非流式调用可恢复错误重试耗尽（详见 LLM 日志）"
            stream_result.error = msg
            yield build_error_event(f"LLM 调用失败: {msg}")
            return

        # 成功：generate() 返回独立 StreamResult（字段与流式整流合并后同构——
        # content/reasoning_content/has_reasoning/finish_reason/tool_calls/usage/refusal）
        for key in (
            "content",
            "reasoning_content",
            "has_reasoning",
            "finish_reason",
            "tool_calls",
            "usage",
            "refusal",
        ):
            setattr(stream_result, key, getattr(result, key))
        # 事件合成：reasoning 先 message（对齐 thinking 模型产出序）；空串不产事件
        if result.reasoning_content:
            yield build_reasoning_event(result.reasoning_content)
        if result.content:
            yield build_message_event(result.content)

    def _take_over_failure(
        self,
        current_result: StreamResult | None,
        last_visible_result: StreamResult | None,
        total_usage: dict,
        exception_usage: dict | None = None,
    ) -> StreamResult | None:
        """异常出口统一接管：选出可用成果并归账未计 usage（G0-4：异常不得漏记已知事实）。

        三个异常出口（内置 TimeoutError / 三类 LLM 终结信号 / 未分类 Exception）都必须先
        完成接管再组装终态：成果决定 outcome 的 content/reasoning，usage 决定 outcome.usage
        与成本口径。异常自带 usage 时优先且不与 result 中同一笔重复计（见 `_unaccounted_usage`）。
        普通 def（无 await）：只写传入的 total_usage 引用，不发起任何调用。
        """
        terminal_result = _terminal_result(current_result, last_visible_result)
        total_usage.update(merge_usage(total_usage, _unaccounted_usage(current_result, exception_usage)))
        return terminal_result

    # ==================================================================
    # execute 的拆分方法（职责单一，行为与原内联分支一致）
    # 终止信号：设置 self.outcome = 终止；不设置 = 继续循环（主循环据此 return/continue）
    # ==================================================================

    async def _dispatch(self, kind: AgentErrorKind, message: str, iteration: int) -> AgentErrorAction:
        """错误分发（react 内唯一入口）：RAISE 决策抛 AgentRunError，否则返回 action——
        委托共享 _common.dispatch_error。"""
        return await dispatch_error(self._error_handlers, kind, message, iteration)

    def _finalize_outcome(
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
    ) -> list[str]:
        """统一收尾：组装 outcome + 返回收尾事件（可选 info + done 恰一次）。

        覆盖各终结 / STOP 分支的公共尾段（dispatch 由调用方负责——CONTINUE 语义
        各异：重试 / 回喂 / 忽略）。done 的 iterations / total_tokens 从 outcome 取。
        非 async：无 await，直接组事件列表。
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
        events: list[str] = []
        if info_message:
            events.append(build_info_event(info_message))
        events.append(
            build_done_event(
                iterations=self.outcome.iterations,
                total_tokens=self.outcome.total_tokens,
            )
        )
        return events

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
    ) -> list[str]:
        """终结性错误统一收尾：dispatch（RAISE 上抛，CONTINUE 忽略）→ 事件列表。

        供所有「CONTINUE 无恢复语义」的终结分支复用，包括执行护栏、拒答、
        UNKNOWN、达到硬重试上限等。可恢复分支需先取得 action，再按各自的重试
        或回喂语义调用 _finalize_outcome。
        """
        await self._dispatch(kind, message, iteration)
        return self._finalize_outcome(
            success=success,
            content=content,
            reasoning=reasoning,
            iteration=iteration,
            total_usage=total_usage,
            error=error,
            info_message=info_message,
            structured=structured,
        )

    async def _handle_llm_failed(
        self,
        stream_result: StreamResult,
        iteration: int,
        total_usage: dict,
        max_llm_fail_retries: int,
    ) -> list[str]:
        """LLM 调用失败 → 错误分发（默认 STOP 短路；handler 可重试/上抛）。

        返回收尾事件列表；终止与否由主循环据 self.outcome 判定。
        CONTINUE 重试受 max_llm_fail_retries 上限护栏：连续失败超上限后硬终止——
        即使 handler 返回 CONTINUE 也不继续（防 handler 配置失误 / LLM 持续失败
        时无限重试烧钱）。
        """
        # 硬终止分支：先 dispatch（handler 可 RAISE 上抛），STOP/CONTINUE 均终止
        if self._llm_fail_retries > max_llm_fail_retries:
            error = f"连续 LLM 调用失败（{self._llm_fail_retries} 轮），已终止"
            return await self._finalize_terminal(
                kind=AgentErrorKind.LLM_FAILED,
                message=error,
                iteration=iteration,
                success=False,
                content=stream_result.content,
                reasoning=stream_result.reasoning_content,
                total_usage=total_usage,
                error=error,
                info_message=error,
            )

        action = await self._dispatch(AgentErrorKind.LLM_FAILED, stream_result.error or "", iteration)
        if action == AgentErrorAction.STOP:
            return self._finalize_outcome(
                success=False,
                content=stream_result.content,
                reasoning=stream_result.reasoning_content,
                iteration=iteration,
                total_usage=total_usage,
                error=stream_result.error,
            )

        return [build_info_event(f"LLM 失败，按错误处理策略重试: {stream_result.error}")]

    async def _finalize_refused(
        self,
        stream_result: StreamResult,
        iteration: int,
        total_usage: dict,
    ) -> list[str]:
        """模型拒答 → 错误分发（默认 STOP；不误判为成功答案、不空转重试）。

        显式拒答信号（refusal 字段 / content_filter，LLM-004 原则）。拒答文本
        截断（LLM-008 基线：拒答常引用触发内容，完整文本不落盘）。
        """
        reason = stream_result.refusal or "内容安全策略触发（content_filter）"
        error = f"模型拒答: {reason[:200]}"
        return await self._finalize_terminal(
            AgentErrorKind.REFUSED,
            error,
            iteration,
            success=False,
            content=stream_result.content,
            reasoning=stream_result.reasoning_content,
            total_usage=total_usage,
            error=error,
            info_message=error,
        )

    async def _dispatch_tool_protocol_failure(
        self,
        kind: AgentErrorKind,
        message: str,
        iteration: int,
        max_tool_protocol_retries: int,
    ) -> tuple[AgentErrorAction, str | None]:
        """登记一次连续协议异常并分发，返回（动作，硬终止原因）。

        三类会触发新 LLM 修正轮的错误共享此预算。超过上限时仍调用当前 kind
        的处理器，让 RAISE 保持可观察；STOP/CONTINUE 统一折算为 STOP。
        """
        self._tool_protocol_retries += 1
        if self._tool_protocol_retries > max_tool_protocol_retries:
            hard_error = f"连续工具调用协议异常（{self._tool_protocol_retries} 轮），已终止"
            await self._dispatch(kind, hard_error, iteration)
            return AgentErrorAction.STOP, hard_error
        return await self._dispatch(kind, message, iteration), None

    async def _handle_tool_protocol_error(
        self,
        message: str,
        full_reasoning: str,
        iteration: int,
        total_usage: dict,
        max_tool_protocol_retries: int,
    ) -> list[str]:
        """工具调用信号/能力不一致 → PARSE_FAILED 分发并受协议修正上限约束。

        无效响应不进入消息历史，也不参与空输出计数或停滞检测。默认 CONTINUE
        修正下一轮；达到硬上限时 STOP/CONTINUE 均终止，RAISE 仍可上抛。
        """
        action, hard_error = await self._dispatch_tool_protocol_failure(
            AgentErrorKind.PARSE_FAILED,
            message,
            iteration,
            max_tool_protocol_retries,
        )
        if action == AgentErrorAction.STOP:
            return self._finalize_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                error=hard_error or message,
                info_message=hard_error,
            )

        return [build_info_event(f"{message}，按错误处理策略重试")]

    async def _handle_final_answer(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        iteration: int,
        output_schema: dict,
        total_usage: dict,
        full_reasoning: str,
        max_tool_protocol_retries: int,
    ) -> list[str]:
        """final_answer 工具：成功提取终止 / 校验失败分发（CONTINUE 回喂）。

        返回收尾事件列表；主循环仅在检测到 final_answer 调用时调用本方法，
        终止与否据 self.outcome 判定（成功置位；校验失败 CONTINUE 不置位继续）。
        """
        final_tcs = [tc for tc in tool_calls if tool_call_name(tc) == _FINAL_ANSWER_TOOL]
        structured, err = extract_final_answer(final_tcs[0], output_schema)
        if structured is not None:
            self._tool_protocol_retries = 0
            # 成功：终止循环，结构化进 outcome
            return self._finalize_outcome(
                success=True,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                structured=structured,
                info_message="已收到结构化最终答案",
            )

        # 校验失败 → 错误分发（默认 CONTINUE 回喂；handler 可终止/上抛）
        fa_msg = f"final_answer 参数{err}"
        action, hard_error = await self._dispatch_tool_protocol_failure(
            AgentErrorKind.STRUCTURED_INVALID,
            fa_msg,
            iteration,
            max_tool_protocol_retries,
        )
        if action == AgentErrorAction.STOP:
            return self._finalize_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                error=hard_error or "final_answer 参数校验失败（按错误处理策略终止）",
                info_message=hard_error,
            )

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
        return [build_info_event(f"final_answer 校验失败，已回喂: {err}")]

    def _bump_stall(self, tool_calls: list[dict]) -> int:
        """更新停滞指纹并返回连续相同动作轮数（状态留在策略实例字段上）。

        指纹 = 工具名 + 规范化参数（见 `action_fingerprint`，「换工具 / 换参数」即指纹变化）：
        相同动作连续累计，其余情况重置为 1。是否越限的判定与终态分别由主循环与
        `_finalize_stalled` 负责，本方法只维护计数，不产生副作用。
        """
        fp = action_fingerprint(tool_calls)
        if fp and fp == self._last_action_fp:
            self._stall_count += 1
        else:
            self._stall_count = 1
            self._last_action_fp = fp
        return self._stall_count

    async def _finalize_stalled(
        self,
        tool_calls: list[dict],
        iteration: int,
        total_usage: dict,
        full_reasoning: str,
        count: int,
    ) -> list[str]:
        """连续相同工具调用超限 → 错误分发（默认 STOP 停机；handler 可上抛）。

        CONTINUE 被忽略（同 TIMEOUT / COST_EXCEEDED），RAISE 由 _dispatch 抛出。
        检测点在工具执行前：本方法返回即终止，本轮工具不执行。
        """
        names = "、".join(tool_call_name(tc) for tc in tool_calls)
        error = f"连续 {count} 轮相同工具调用（{names}），已终止"
        # 停机组装 outcome（本轮无工具执行，content 空，保留 reasoning）
        return await self._finalize_terminal(
            AgentErrorKind.STALLED,
            error,
            iteration,
            success=False,
            content="",
            reasoning=full_reasoning.strip(),
            total_usage=total_usage,
            error=error,
            info_message=error,
        )

    async def execute_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        iteration: int,
        *,
        run: ReasoningRunScope,
        tool_execution: ToolExecutionOptions = _DEFAULT_TOOL_EXECUTION,
        deadline: float | None = None,
        cleanup_deadline: float | None = None,
        batch_cleanup_grace: float = DEFAULT_BATCH_CLEANUP_GRACE,
        assistant_message: dict | None = None,
    ) -> AsyncGenerator[str]:
        """
        并行执行工具调用列表，追加结果到 messages，记录到 _tool_call_records。

        **并发执行**：ToolBatchRunner 逐项接管结局（并发度由 ToolService 的
        ToolAdmission 全局/单运行准入限制）；ReAct 按输入顺序提交
        assistant.tool_calls 与 tool 回执，避免部分完成或取消后产生非法历史。

        **独立使用场景**：需要直接执行已给定工具调用的策略或测试。PlannerStrategy 和
        ReflectionStrategy 当前复用完整 execute()，不通过本原语承担子流程生命周期。

        **tool_execution**：透传给 ToolGateway.execute（字段为 None = 走执行器全局或工具
        自声明，见 execute() docstring）——供原语复用方按需覆盖，默认不覆盖任何一项。

        **batch_cleanup_grace**：首个控制异常后给在途兄弟的收尾上界，实际取其与
        `cleanup_deadline` 的较小值。由 `execute` 从 ExecutionLimits 传入（生产值来自配置），
        本层不读配置；直接复用本原语时走模块默认。

        SSE 事件只在主 generator 内按顺序 yield（不在并发 task 内 yield， 避免事件交错）。

        Yields:
            tool_call / tool_result SSE 事件

        Raises:
            ValueError: 批内调用身份非法（id 缺失或重复），在任何真实 Gateway 请求前抛出
            BaseException: 批次整体失败（如硬取消）——在历史已提交、事件尚未发出的位置
                原样重抛；事件发完后若无控制异常，则上抛首个未预期异常
            ToolCancelledError / ToolDeadlineExceededError / ToolRunStoppedError: 三类运行级
                控制异常按固定优先级上抛（取消 > 超时 > 停跑），终态由上层按异常类型决定
        """

        # 身份检查放在真正执行之前：批内 id 缺失或重复时，tool 回执无法与 assistant.tool_calls
        # 配对，与其让网关在下一轮返回 400，不如在这里直接失败，定位更直接。
        identity_error = tool_call_identity_error(tool_calls)
        if identity_error is not None:
            raise ValueError(identity_error)

        # 整批共用一个 batch_id（便于按批次检索事实），但每个工具各有一个 operation_id：
        # batch 表示「这一次一共调了几把工具」，operation 才是「一个业务操作」的身份。
        batch_id = uuid.uuid4().hex
        # 取消来源合并成一条链：父运行的取消 + 本运行的取消，任一触发都要停下手上的工具。
        # 本运行没有 cancel_event 时不要往元组里塞 None——元组里只允许放 Event。
        cancel_events = (
            *run.parent_cancel_events,
            *((run.cancel_event,) if run.cancel_event is not None else ()),
        )
        # 先把每个工具的调用上下文（身份 + 期限 + 取消链）全部建好，再开始并发执行。
        # 这样并发任务只需按索引取自己那份，彼此之间不共享任何可变状态。
        contexts: list[ToolCallContext] = []
        for tc in tool_calls:
            tool_call_id = tc["id"]
            call = ToolCallContext(
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                batch_id=batch_id,
                tool_call_id=tool_call_id,
                operation_id=uuid.uuid4().hex,
                deadline=deadline,
                cleanup_deadline=cleanup_deadline,
                cancel_events=cancel_events,
                run_stop=run.run_stop,
            )
            contexts.append(call)

        # 提前取出网关引用：闭包和后面的编排共用同一个执行入口，不再反复读 self。
        gateway = self._tools

        async def _execute_one(index: int) -> tuple:
            """并行执行单个工具（并发 task 内只做执行，不 yield 事件）。"""
            # 本函数跑在并发 task 里，只做「解析参数 → 调用工具」两件事：绝不在里面 yield
            # 事件，否则多个工具的事件会互相交错。参数与上下文一律按 index 取自己的那一份，
            # 不依赖外层循环变量（那时循环早已推进到下一轮）。
            tc = tool_calls[index]
            tool_name = tool_call_name(tc)
            call = contexts[index]

            # 计时涵盖参数解析 + 工具执行，仅供 SSE 事件与证据链展示，不参与任何预算判定。
            start = time.monotonic()
            try:
                raw_args = tc["function"]["arguments"]
                tool_args = json.loads(raw_args)
            except (json.JSONDecodeError, KeyError) as e:
                # 参数 JSON 解析失败：不静默用空参执行（会掩盖错误、可能触发副作用），
                # 构造失败 ToolResult 走失败回喂分支——模型可见原因自纠，JSON_PARSE 进证据链。
                raw_args = tc.get("function", {}).get("arguments", "")
                tool_args = {}
                exec_result = ToolResult(
                    success=False,
                    content="",
                    error=f"参数 JSON 解析失败: {e!s}（原始参数: {raw_args[:200]}）",
                    error_code=ErrorCode.JSON_PARSE,
                )
                # 工具确实没启动，也要在事实链里留一条（revision=1 覆盖启动前预登记的 revision=0），
                # 否则「因为参数错所以没执行」这件事在事后的事实里查不到。
                collector.record(
                    ToolFact(
                        operation_id=call.operation_id,
                        run_id=call.run_id,
                        batch_id=call.batch_id,
                        tool_call_id=call.tool_call_id,
                        revision=1,
                        execution_state=ToolExecutionState.NOT_STARTED,
                        effect_state=ToolEffectState.NONE,
                        cleanup_state=ToolCleanupState.NOT_NEEDED,
                        result=exec_result,
                    )
                )
            else:
                # 正常路径：交给网关执行。call 传本次调用的身份与期限；collector 是事实收集器，
                # 执行过程中的预登记 / 每次尝试 / 最终结果都由它接管。
                # timeout / max_retries 来自 tool_execution，为 None 表示交给执行器按工具自声明或全局配置决定。
                exec_result = await gateway.execute(
                    tool_name,
                    tool_args,
                    timeout=tool_execution.timeout,
                    max_retries=tool_execution.max_attempts,
                    call=call,
                    facts=collector,
                )

            # 无论走哪条分支都从同一出口返回：结果 + 回执用参数 + 耗时（调用方按索引拿工具名）。
            return exec_result, tool_args, time.monotonic() - start

        # ToolBatchRunner 负责并发调度与「逐项接管结局」：谁先完成先收谁；出现控制类异常时
        # 给兄弟任务留一段有界收尾时间，不让它们被硬砍在半路。事实归 ToolBatchCollector。
        collector = ToolBatchCollector()
        runner = ToolBatchRunner(collector)
        # 先接住异常而不是让它直接冒出去：不论成功还是失败，下面的 finally 都要先把已经拿到的
        # 事实存下来并组装回执，不能出现「工具跑了、历史里却没有回执」这种缺口。
        execution_error: BaseException | None = None
        try:
            await runner.run(
                contexts,
                _execute_one,
                cleanup_deadline=cleanup_deadline,
                batch_cleanup_grace=batch_cleanup_grace,
            )
        except BaseException as error:  # noqa: BLE001 -- 硬取消也须先提交可得批次事实和协议回执
            execution_error = error
        finally:
            # 先保存独立事实；迟回只归 Integration，不再引用本批次的 Agent。
            facts = collector.snapshot()
            self._tool_facts.extend(facts)
            collector.close()

        # 事实按 tool_call_id 分桶，方便下面每个工具只找自己的那几条（可能有预登记、多次尝试多条）。
        # 先分好桶再进循环，避免每个工具都全表扫一遍。
        facts_by_call: dict[str, list[ToolFact]] = {}
        for fact in facts:
            facts_by_call.setdefault(fact.tool_call_id, []).append(fact)
        # 三类产物分开攒，最后按固定顺序一次性提交（原因见方法末尾）：
        # events = 发给消费者的 SSE 事件；tool_messages = 回喂模型的历史；
        # control_errors / unexpected_errors = 需要上抛、由上层决定终态的异常。
        events: list[str] = []
        tool_messages: list[dict] = []
        control_errors: list[BaseException] = []
        unexpected_errors: list[BaseException] = []
        # 按模型给出的顺序（tool_calls 的顺序）逐个组装，不按完成顺序——历史里
        # assistant.tool_calls 与 tool 回执必须同序一一配对，否则下一轮请求非法。
        for index, tc in enumerate(tool_calls):
            # outcome 是 ToolBatchRunner 逐项接管的结局，有三种可能：
            #   元组            = 正常返回（_execute_one 的返回值）
            #   异常对象        = 这个工具抛了（取消 / 超时 / 停跑 / 其他意外）
            #   None            = 任务还挂着没结束（被取消但吞掉了取消，真实线程未停）
            outcome = runner.outcomes[index]
            call = contexts[index]
            tool_name = tc.get("function", {}).get("name", "unknown")
            if isinstance(outcome, tuple):
                # 正常返回：结果、回执参数、耗时都在元组里。
                exec_result, tool_args, elapsed = outcome
            else:
                # 非正常返回：没有返回值可用，只能从已接管的事实快照还原出回执——
                # 要么「未执行」，要么「结果尚未确认」。参数按模型输入尽力解析。
                exec_result, tool_args, elapsed = _aborted_call_outcome(tc, facts_by_call.get(call.tool_call_id, []))
                # 异常本身先记下来、留到整批回执提交完再上抛：三类控制异常由上层决定终态，
                # 其余意外异常也不能吞掉，但优先级低于控制异常。
                if isinstance(outcome, (ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError)):
                    control_errors.append(outcome)
                elif isinstance(outcome, BaseException):
                    unexpected_errors.append(outcome)

            # 先发 tool_call（模型打算调什么），紧跟 tool_result（实际结果）：逐条按输入顺序发出，
            # 与下面写进历史的顺序一致，前端因此不会看到"结果先于调用"。
            events.append(build_tool_call_event(tool_name, tool_args, iteration))

            # 回喂模型：成功回喂 content，失败回喂 str(result)（"错误: <error>"）——
            # 模型需看到失败原因才能自愈（工具失败空串回喂是核心缺口）。
            # error / error_code 同时进证据链记录（根因报告要能看到失败原因与分类）。
            feedback = exec_result.content if exec_result.success else str(exec_result)

            # 这条记录有两个用途，改动字段前两处都要看：
            #   1) 随后由 outcome.tool_calls 交给上层做证据链（保留完整结果与错误分类）；
            #   2) _handle_tool_calls 靠它在本批新增记录里筛出失败项做错误分发，
            #      所以 success / error_code 的语义是分发依据，不能随手改。
            self._tool_call_records.append(
                {
                    "tool": tool_name,
                    "params": tool_args,
                    "result": exec_result.content,
                    "success": exec_result.success,
                    "error": exec_result.error,
                    "error_code": (exec_result.error_code.value if exec_result.error_code else None),
                    "duration": round(elapsed, 3),
                }
            )

            # 同一条回喂文本按用途截到不同长度：SSE 事件给前端看，200 字符够用；
            # 回喂模型的消息要留更多细节（2000），否则模型看不到足够信息无法自纠。
            events.append(
                build_tool_result_event(
                    tool_name,
                    _truncate_with_marker(feedback, 200),
                    elapsed,
                    iteration,
                )
            )

            # tool 消息必须带 tool_call_id，与 assistant 消息里的 tool_calls 一一配对；
            # 少配一个，下一次请求就会被网关以 400 拒绝。
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": _truncate_with_marker(feedback, 2000),
                }
            )

        # 提交顺序是刻意的：先把历史整批写好（assistant.tool_calls + 全部 tool 回执），
        # 再抛异常，最后才 yield 事件。这样消费者拿到第一条事件时，历史已经完整落盘，
        # 它无论何时关闭生成器都不会留下「有 tool 回执却没有对应 assistant」的半截历史。
        #
        # assistant 消息优先用调用方传进来的那份（execute 在第 8 步已经组装好）；只有没传时
        # 才自己补一条。补之前再查一次历史末尾：如果已经是与本批完全相同的 assistant.tool_calls
        # （例如同一批被重复提交），就不要再写第二条，否则配对会重复。
        if assistant_message is None and not (
            messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls") == tool_calls
        ):
            assistant_message = {"role": "assistant", "content": "", "tool_calls": tool_calls}
        if assistant_message is not None:
            messages.append(assistant_message)
        messages.extend(tool_messages)
        # 批次整体失败（例如硬取消）在这里上抛：历史已经提交，工具跑出来的进展不会被丢掉。
        if execution_error is not None:
            raise execution_error
        for event in events:
            yield event
        # 事件发完才处理异常：三类控制异常按固定优先级上抛（取消 > 超时 > 停跑），
        # 使终态归因不随工具的完成顺序变化。
        for error_type in (ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError):
            control = next((error for error in control_errors if isinstance(error, error_type)), None)
            if control is not None:
                raise control
        # 没有控制异常时才抛未预期异常；有控制异常时它被掩盖是有意的——控制异常是更准确的归因。
        if unexpected_errors:
            raise unexpected_errors[0]

    async def _handle_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        iteration: int,
        total_usage: dict,
        full_reasoning: str,
        tool_execution: ToolExecutionOptions,
        max_tool_protocol_retries: int,
        *,
        run: ReasoningRunScope,
        deadline: float | None,
        cleanup_deadline: float | None,
        batch_cleanup_grace: float,
        assistant_message: dict | None,
    ) -> AsyncGenerator[str]:
        """工具执行 + 可恢复错误分发（默认 CONTINUE 继续）。"""

        before = len(self._tool_call_records)

        # ----- 工具执行 -----
        async for event in self.execute_tool_calls(
            tool_calls,
            messages,
            iteration,
            tool_execution=tool_execution,
            run=run,
            deadline=deadline,
            cleanup_deadline=cleanup_deadline,
            batch_cleanup_grace=batch_cleanup_grace,
            assistant_message=assistant_message,
        ):
            yield event

        # ----- 可恢复错误分发 -----
        new_failures = [r for r in self._tool_call_records[before:] if not r.get("success")]
        grouped = _group_failures_by_kind(new_failures)
        if AgentErrorKind.PARSE_FAILED not in grouped:
            # 无解析错误说明工具参数可被正确解析即工具调用协议已恢复；业务失败属于另一语义。
            self._tool_protocol_retries = 0

        if grouped:
            decisions: list[tuple[AgentErrorKind, str, AgentErrorAction]] = []
            # 分发顺序固定：协议类在前。它带独立预算且可能硬终止，终局必须先定——
            # 否则同轮业务失败仍会被分发，其 RAISE 会把预算诊断顶成无关错误，STOP 归因
            # 也会被硬终止错误覆盖（grouped 的插入序还取决于工具的返回顺序）。
            for kind in (AgentErrorKind.PARSE_FAILED, AgentErrorKind.TOOL_FAILED):
                fails = grouped.get(kind)
                if not fails:
                    continue

                # 同 kind 的多个失败聚合为一条 message
                fail_msg = "；".join(f"{f.get('tool', '?')}: {f.get('error', '')}" for f in fails)

                # 按 kind 分发，_dispatch 遇到 RAISE 会立即抛出；其余决策再按 STOP > CONTINUE 仲裁。
                hard_protocol_error: str | None = None
                if kind == AgentErrorKind.PARSE_FAILED:
                    (
                        action,
                        hard_protocol_error,
                    ) = await self._dispatch_tool_protocol_failure(
                        kind,
                        fail_msg,
                        iteration,
                        max_tool_protocol_retries,
                    )
                else:
                    action = await self._dispatch(kind, fail_msg, iteration)

                if hard_protocol_error is not None:
                    # 预算耗尽：终局已定，同轮剩余 kind 不再分发——STOP/CONTINUE 改变不了终局，
                    # RAISE 只会用一个无关错误顶掉这条预算诊断。
                    for e in self._finalize_outcome(
                        success=False,
                        content="",
                        reasoning=full_reasoning.strip(),
                        iteration=iteration,
                        total_usage=total_usage,
                        error=hard_protocol_error,
                        info_message=hard_protocol_error,
                    ):
                        yield e
                    return

                decisions.append((kind, fail_msg, action))

            # RAISE 已在 _dispatch 中传播，因此 decisions 只包含 STOP / CONTINUE，任何 STOP 都终止。
            # 终止/上报时其他失败不回喂模型（循环结束，回喂无意义），但全部失败已进证据链。
            for kind, fail_msg, action in decisions:
                if action == AgentErrorAction.STOP:
                    for e in self._finalize_outcome(
                        success=False,
                        content="",
                        reasoning=full_reasoning.strip(),
                        iteration=iteration,
                        total_usage=total_usage,
                        error=f"{kind.value}（按错误处理策略终止）: {fail_msg}",
                    ):
                        yield e
                    return

            # 全 CONTINUE：工具结果已回喂，继续循环
            return

    async def _handle_empty_output(
        self,
        full_reasoning: str,
        iteration: int,
        total_usage: dict,
        max_empty_retries: int,
    ) -> list[str]:
        """空输出 → 错误分发（默认 CONTINUE 重试；handler 可终止/上抛）。

        返回收尾事件列表；终止与否由主循环据 self.outcome 判定。
        连续空输出重试上限：计数超过 max_empty_retries 后硬终止——即使 handler
        返回 CONTINUE 也不继续（防模型空转烧钱）。
        """
        # 硬终止分支：先 dispatch（handler 可 RAISE 上抛），STOP/CONTINUE 均终止
        if self._empty_retries > max_empty_retries:
            error = f"连续空输出（{self._empty_retries} 轮），已终止"
            return await self._finalize_terminal(
                AgentErrorKind.EMPTY_OUTPUT,
                error,
                iteration,
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                total_usage=total_usage,
                error=error,
                info_message=error,
            )

        action = await self._dispatch(AgentErrorKind.EMPTY_OUTPUT, "LLM 未生成有效输出", iteration)
        if action == AgentErrorAction.STOP:
            return self._finalize_outcome(
                success=False,
                content="",
                reasoning=full_reasoning.strip(),
                iteration=iteration,
                total_usage=total_usage,
                error="LLM 未生成有效输出（按错误处理策略终止）",
                info_message="LLM 未生成有效输出（按错误处理策略终止）",
            )

        # CONTINUE（默认）：重试
        return [build_info_event("LLM 未生成有效输出，重试")]

    async def _finalize_max_turns(
        self,
        last_visible_result: StreamResult | None,
        max_iterations: int,
        total_usage: dict,
    ) -> list[str]:
        """达到最大迭代次数 → 错误分发（默认 STOP 兜底；handler 可上抛）。"""
        error = f"已达到最大迭代次数({max_iterations})"
        # STOP（默认）：现有兜底（CONTINUE 循环已耗尽，按 STOP 处理）
        return await self._finalize_terminal(
            AgentErrorKind.MAX_TURNS,
            error,
            max_iterations,
            success=(bool(last_visible_result.content.strip()) if last_visible_result else False),
            content=(last_visible_result.content.strip() if last_visible_result else ""),
            reasoning=(last_visible_result.reasoning_content.strip() if last_visible_result else ""),
            total_usage=total_usage,
            error=error,
            info_message=error,
        )

    async def _finalize_guard_result(
        self,
        guard: GuardResult,
        result: StreamResult | None,
        iteration: int,
        total_usage: dict,
        max_execution_time: float | None,
    ) -> list[str]:
        """把共享护栏判定映射到 ReAct 的终态文案与错误分发。"""
        if guard.kind == AgentErrorKind.CANCELLED:
            error = "Agent 已被取消"
        elif guard.kind == AgentErrorKind.TIMEOUT:
            error = f"ReAct 执行超时（超过 {max_execution_time} 秒）"
        elif guard.kind == AgentErrorKind.COST_EXCEEDED:
            if guard.cost_usd is None:
                raise ValueError("COST_EXCEEDED 护栏缺少 cost_usd")
            error = f"ReAct 执行成本超限（累计 ${guard.cost_usd:.4f}）"
        elif guard.kind == AgentErrorKind.CONTEXT_EXCEEDED:
            error = guard.message
        else:
            raise ValueError(f"不支持的护栏类型: {guard.kind}")

        return await self._finalize_terminal(
            guard.kind,
            error,
            iteration,
            success=bool(result.content.strip()) if result else False,
            content=result.content.strip() if result else "",
            reasoning=result.reasoning_content.strip() if result else "",
            total_usage=total_usage,
            error=error,
            info_message=error,
        )

    async def _finalize_unknown(
        self,
        last_visible_result: StreamResult | None,
        iteration: int,
        total_usage: dict,
        exc: Exception,
    ) -> list[str]:
        """未捕获异常 → 错误分发（默认 STOP；保留部分进度）。

        对齐护栏终态降级：调用方先经 `_take_over_failure` 从 current_result /
        last_visible_result 中选择 terminal_result 并归账当前轮 usage，本方法据此组装
        outcome（保留已执行工具证据链 + 部分内容）。asyncio.CancelledError / GeneratorExit
        是 BaseException，不被主循环 except Exception 捕获（保持 CANCELLED / 生成器关闭语义）。

        error 脱敏：只保留异常类型名（分类），不拼接异常 message——异常文本可能含
        内部路径 / 参数 / 敏感值 / 堆栈提示，产品可见文本（outcome.error / SSE /
        根因报告）与运维诊断分离：完整异常（含 traceback）进日志，不落产品侧。
        """
        # 属非关键观测：本行紧随终态组装，其失败不得让 UNKNOWN 终态与已接管进度丢失
        # （G0-6）；此处刻意不走 await，避免在终态判定与提交之间新增可取消点。
        isolate_observation(lambda: _logger.error("Agent 运行异常: %s", exc, exc_info=exc))
        error = f"Agent 运行异常: {type(exc).__name__}"
        return await self._finalize_terminal(
            AgentErrorKind.UNKNOWN,
            error,
            iteration,
            success=(bool(last_visible_result.content.strip()) if last_visible_result else False),
            content=(last_visible_result.content.strip() if last_visible_result else ""),
            reasoning=(last_visible_result.reasoning_content.strip() if last_visible_result else ""),
            total_usage=total_usage,
            error=error,
            info_message=error,
        )
