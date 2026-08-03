# ============================================
# core/agent/executor.py - ReAct 执行引擎
# ============================================
"""
ReAct Agent 实现
===============
推理（Reason）→ 行动（Act）→ 观察（Observe），循环直到完成。

事件流输出设计：
    LLM 原始流 → type=reasoning（逐 token）
               → type=message（逐 token）
    Agent 发现 tool_calls → type=tool_call
    执行工具            → type=tool_result
    LLM 下一轮原始流     → type=reasoning / message
    Agent 完成           → type=done

每次 run() 是独立的，上下文通过 AgentContext 传入。
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from app.core.events import (
    build_done_event,
    build_info_event,
    build_tool_call_event,
    build_tool_result_event,
)
from app.services import LLMService, ToolService
from app.services.llm_service import StreamResult

from .base import AgentResult, BaseAgent


class ReActAgent(BaseAgent):
    """
    ReAct 策略实现。

    循环流程：
        1. LLM 推理（流式输出 reasoning / message）
        2. 检查 finish_reason
           - "stop"       → 生成最终结果，结束循环
           - "length"     → 生成部分结果，结束循环
           - "tool_calls" → 执行工具，追加结果到 messages，继续循环
        3. 达到最大迭代次数 → 强制结束
    """

    def __init__(self, llm: LLMService, tools: ToolService) -> None:
        super().__init__(llm, tools)
        self._tool_call_records: list[dict[str, Any]] = []

    async def _strategy_cycle(
        self,
        user_input: str,
        messages: list[dict[str, str]],
    ) -> AsyncGenerator[str]:
        """ReAct 主循环"""
        ctx = self._context
        if ctx is None:
            raise RuntimeError("AgentContext 未设置")

        tool_defs = self._tools.get_openai_tools() if self._tools else None
        has_tools = bool(tool_defs)

        last_result: StreamResult | None = None
        total_usage: dict = {}

        for iteration in range(1, ctx.max_iterations + 1):
            yield build_info_event(f"第 {iteration} 轮推理")

            # ----- 1. LLM 推理 -----
            stream_result = StreamResult()

            async for event in self._llm.async_generate(
                messages=messages,
                tools=tool_defs,
                temperature=ctx.temperature,
                max_tokens=ctx.max_tokens,
                result=stream_result,
            ):
                yield event

            last_result = stream_result
            # 累计 token 用量
            if stream_result.usage:
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    total_usage[k] = total_usage.get(k, 0) + stream_result.usage.get(k, 0)

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
                async for event in self._execute_tool_calls(
                    stream_result.tool_calls,
                    messages,
                    iteration,
                ):
                    yield event
                continue

            # ----- （2）stop / length / 有内容 → 正常结束
            if finish_reason in ("stop", "length") or full_content.strip():
                self._result = self._build_result(
                    full_content,
                    full_reasoning,
                    iteration,
                    total_usage,
                )
                yield build_done_event(iterations=iteration, total_tokens=total_usage.get("total_tokens", 0))
                return

            # ----- （3）空输出 → 重试
            yield build_info_event("LLM 未生成有效输出，重试")

        # ----- 达到最大迭代次数 -----
        yield build_info_event(f"已达到最大迭代次数({ctx.max_iterations})")
        if last_result:
            self._result = self._build_result(
                last_result.content,
                last_result.reasoning_content,
                ctx.max_iterations,
                total_usage,
            )
        else:
            self._result = AgentResult(
                success=False,
                content="",
                iterations=ctx.max_iterations,
                error="LLM 未返回任何结果",
            )
        yield build_done_event(iterations=ctx.max_iterations, total_tokens=total_usage.get("total_tokens", 0))

    async def _execute_tool_calls(
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
            except (json.JSONDecodeError, KeyError):
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

    def _build_result(
        self,
        content: str,
        reasoning: str,
        iterations: int,
        usage: dict | None = None,
    ) -> AgentResult:
        total_tokens = (usage or {}).get("total_tokens", 0)
        return AgentResult(
            success=bool(content.strip()),
            content=content.strip(),
            reasoning=reasoning.strip(),
            tool_calls=self._tool_call_records,
            iterations=iterations,
            total_tokens=total_tokens,
            usage=usage or None,
        )
