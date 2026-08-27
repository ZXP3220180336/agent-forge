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

from app.domain.ports.llm_gateway import LLMGateway, StreamResult
from app.domain.ports.tool_gateway import ToolGateway
from app.shared.events import (
    build_done_event,
    build_info_event,
    build_tool_call_event,
    build_tool_result_event,
)


@dataclass
class ReActOutcome:
    """ReAct 策略执行的最终结果载体（供桥接方组装 AgentResult）。"""

    content: str = ""
    reasoning: str = ""
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

    def __init__(self, llm: LLMGateway, tools: ToolGateway) -> None:
        self._llm = llm
        self._tools = tools
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
    ) -> AsyncGenerator[str]:
        """
        ReAct 主循环。

        循环流程：
            1. LLM 推理（流式输出 reasoning / message）
            2. 检查 finish_reason
               - "stop"       → 生成最终结果，结束循环
               - "length"     → 生成部分结果，结束循环
               - "tool_calls" → 执行工具，追加结果到 messages，继续循环
            3. 达到最大迭代次数 → 强制结束
            4. 达到 max_execution_time（总时长上限）→ 超时降级结束

        Args:
            user_input: 用户原始输入（保留兼容，循环内部以 messages 为准）
            messages: 可修改的消息列表副本
            max_iterations: 最大迭代轮数
            temperature: LLM 采样温度
            max_tokens: 单轮最大输出 token
            max_execution_time: 整个循环总时长上限（秒），None=不设限（向后兼容）；
                超时对齐 max_iterations 兜底模式降级，error 记录超时原因

        Yields:
            SSE 事件字符串（reasoning / message / tool_call / tool_result / info / done）
        """
        tool_defs = self._tools.get_openai_tools() if self._tools else None
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
                            total_usage[k] = total_usage.get(k, 0) + stream_result.usage.get(
                                k, 0
                            )

                    # LLM 调用失败（create 失败 / 流中断放弃 / 用户取消）→ 短路返回失败结果，
                    # 不把「失败」当「空输出」继续空转重试（浪费 LLM 调用 + 错误信息不准确）。
                    # 正常空回（stop + 空 content）error 为 None，仍走下方「空输出重试」逻辑。
                    if stream_result.error:
                        self.outcome = ReActOutcome(
                            success=False,
                            content=stream_result.content,
                            reasoning=stream_result.reasoning_content,
                            iterations=iteration,
                            total_tokens=total_usage.get("total_tokens", 0),
                            usage=total_usage or None,
                            error=stream_result.error,
                        )
                        yield build_done_event(
                            iterations=iteration,
                            total_tokens=total_usage.get("total_tokens", 0),
                        )
                        return

                    full_reasoning = stream_result.reasoning_content
                    full_content = stream_result.content

                    # ----- 2. 将 LLM 回复追加到消息历史 -----
                    assistant_msg: dict = {
                        "role": "assistant",
                        "content": full_content,
                    }
                    if full_reasoning:
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
                        async for event in self.execute_tool_calls(
                            stream_result.tool_calls,
                            messages,
                            iteration,
                        ):
                            yield event
                        continue

                    # ----- （2）stop / length / 有内容 → 正常结束
                    if finish_reason in ("stop", "length") or full_content.strip():
                        self.outcome = ReActOutcome(
                            success=bool(full_content.strip()),
                            content=full_content.strip(),
                            reasoning=full_reasoning.strip(),
                            tool_calls=self._tool_call_records,
                            iterations=iteration,
                            total_tokens=total_usage.get("total_tokens", 0),
                            usage=total_usage or None,
                        )
                        yield build_done_event(
                            iterations=iteration,
                            total_tokens=total_usage.get("total_tokens", 0),
                        )
                        return

                    # ----- （3）空输出 → 重试
                    yield build_info_event("LLM 未生成有效输出，重试")

                # ----- 达到最大迭代次数 -----
                yield build_info_event(f"已达到最大迭代次数({max_iterations})")
                if last_result:
                    self.outcome = ReActOutcome(
                        success=bool(last_result.content.strip()),
                        content=last_result.content.strip(),
                        reasoning=last_result.reasoning_content.strip(),
                        tool_calls=self._tool_call_records,
                        iterations=max_iterations,
                        total_tokens=total_usage.get("total_tokens", 0),
                        usage=total_usage or None,
                    )
                else:
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
        except TimeoutError:
            # 关闭判别：生成器正被 finalizer/aclose 关闭（不同 task 驱动）或外部取消
            # → 干净停止，不 yield 降级事件（避免 RuntimeError: async generator ignored GeneratorExit）
            cur = asyncio.current_task()
            if cur is None or cur is not entered_task or cur.cancelling() > 0:
                return
            # 真超时：对齐 max_iterations 兜底模式，用 last_result 组装降级 outcome
            error = f"ReAct 执行超时（超过 {max_execution_time} 秒）"
            self.outcome = ReActOutcome(
                success=bool(last_result.content.strip()) if last_result else False,
                content=last_result.content.strip() if last_result else "",
                reasoning=last_result.reasoning_content.strip() if last_result else "",
                tool_calls=self._tool_call_records,
                iterations=iteration,
                total_tokens=total_usage.get("total_tokens", 0),
                usage=total_usage or None,
                error=error,
            )
            yield build_info_event(error)
            yield build_done_event(
                iterations=self.outcome.iterations,
                total_tokens=self.outcome.total_tokens,
            )
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
                tool_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError, KeyError:
                tool_args = {}
            start = time.monotonic()
            exec_result = await self._tools.execute(tool_name, tool_args)
            elapsed = time.monotonic() - start
            return exec_result, tool_name, tool_args, tc, elapsed

        # gather 保证结果顺序 = tool_calls 输入顺序
        results = await asyncio.gather(*[_execute_one(tc) for tc in tool_calls])

        tool_messages: list[dict] = []
        for exec_result, tool_name, tool_args, tc, elapsed in results:
            yield build_tool_call_event(tool_name, tool_args, iteration)

            self._tool_call_records.append(
                {
                    "tool": tool_name,
                    "params": tool_args,
                    "result": exec_result.content,
                    "success": exec_result.success,
                    "duration": round(elapsed, 3),
                }
            )

            yield build_tool_result_event(
                tool_name,
                exec_result.content[:200],
                elapsed,
                iteration,
            )

            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": exec_result.content[:2000],
                }
            )

        messages.extend(tool_messages)
