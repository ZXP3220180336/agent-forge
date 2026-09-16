"""聊天用例编排与单次运行所有权。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from app.application.context.context_manager import ContextManager
from app.application.session.session_manager import SessionManager
from app.application.task.task_service import TaskService
from app.domain.agent import AgentContext, ReActAgent
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.shared.events import build_info_event
from app.shared.exceptions import ForbiddenError, NotFoundError
from app.shared.types import SessionId, UserId


@dataclass(slots=True)
class ChatRun:
    """一次已完成流前预检和登记的聊天运行。"""

    session_id: SessionId
    run_id: str
    user_message: str
    messages: list[dict]
    truncated_history: int
    context: AgentContext
    agent: ReActAgent
    cancel_event: asyncio.Event
    _service: ChatService = field(repr=False)
    _events_started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def events(self) -> AsyncGenerator[str]:
        """运行 Agent 并转发事件；每个运行只允许消费一次，提前关闭会请求取消。"""
        if self._closed:
            return
        if self._events_started:
            raise RuntimeError("聊天运行事件流只能消费一次")
        self._events_started = True

        try:
            if self.truncated_history:
                yield build_info_event(
                    "上下文超限，已裁剪最早 "
                    f"{self.truncated_history} 条历史消息以适配模型上下文窗口"
                )

            stream = self._service._task_service.run_agent(
                user_input=self.user_message,
                messages=self.messages,
                context=self.context,
                agent=self.agent,
            )
            async with aclosing(stream):
                async for event in stream:
                    yield event
        except GeneratorExit, asyncio.CancelledError:
            # 消费者主动关闭生成器，或者当前协程被取消
            self.request_cancel()
            raise

    def request_cancel(self) -> None:
        """请求当前运行在既有控制边界停止。"""
        self.cancel_event.set()

    async def aclose(self) -> None:
        """幂等关闭运行：先释放登记，再持久化已接管的非空完整答复。"""
        if self._closed:
            return
        self._closed = True
        await self._service._finalize(self)

    @property
    def closed(self) -> bool:
        """运行登记与结果提交是否已进入最终关闭。"""
        return self._closed


class ChatService:
    """聊天应用用例：预检、运行准备、停止与结果提交。"""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        context_manager: ContextManager,
        task_service: TaskService,
        llm: LLMGateway,
        tools: ToolGateway,
        agent_params: Mapping[str, Any],
        cost_limiter: CostLimiterPort | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._context_manager = context_manager
        self._task_service = task_service
        self._llm = llm
        self._tools = tools
        self._agent_params = dict(agent_params)
        self._cost_limiter = cost_limiter

    async def prepare_message(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        max_iterations: int | None,
    ) -> ChatRun:
        """在响应头前完成授权、用户消息提交、历史快照、运行登记和 Agent 创建。"""

        # 1. 转换身份类型
        sid = SessionId(session_id)
        uid = UserId(user_id)

        # 2. 校验会话归属
        session = await self._session_manager.get_session(sid)
        if not session:
            raise NotFoundError("会话不存在")
        if session["user_id"] != user_id:
            raise ForbiddenError("无权访问该会话")

        # 3. 保存用户消息
        message_id = await self._session_manager.add_message(
            session_id=sid,
            role="user",
            content=message,
            token_count=self._context_manager.count_tokens(message),
        )
        if not isinstance(message_id, int) or message_id <= 0:
            raise RuntimeError("用户消息已写入，但未取得有效消息 ID")

        # 4. 构建本次运行的历史快照
        (
            messages,
            _total,
            truncated_history,
        ) = await self._context_manager.build_messages(
            session_id=sid,
            user_message=message,
            current_message_id=message_id,
        )

        # 5. 创建唯一运行身份和取消事件
        run_id = uuid.uuid4().hex
        cancel_event = self._task_service.create_cancel_event(session_id, run_id)

        try:
            # 6. 创建 AgentContext
            context = AgentContext(
                session_id=sid,
                user_id=uid,
                run_id=run_id,
                run_stop=asyncio.Event(),  # 随运行身份向更深的工具执行链传递，用于阻止该运行继续启动新的工具副作用。
                temperature=self._agent_params["temperature"],
                max_tokens=self._agent_params["max_tokens"],
                max_iterations=(
                    max_iterations
                    if max_iterations is not None
                    else self._agent_params["max_iterations"]
                ),
                max_execution_time=self._agent_params["max_execution_time"],
                max_context_rounds=self._agent_params["max_context_rounds"],
                max_context_tokens=self._agent_params["max_context_tokens"],
                max_empty_retries=self._agent_params["max_empty_retries"],
                max_llm_fail_retries=self._agent_params["max_llm_fail_retries"],
                max_tool_protocol_retries=self._agent_params[
                    "max_tool_protocol_retries"
                ],
                max_same_action_turns=self._agent_params["max_same_action_turns"],
            )

            # 7. 为当前请求创建独立 Agent
            agent = ReActAgent(
                llm=self._llm,
                tools=self._tools,
                context_budget=self._context_manager,
                cost_limiter=self._cost_limiter,
                cancel_event=cancel_event,
            )

            # 8. 返回已登记的 ChatRun
            return ChatRun(
                session_id=sid,
                run_id=run_id,
                user_message=message,
                messages=messages,
                truncated_history=truncated_history,
                context=context,
                agent=agent,
                cancel_event=cancel_event,
                _service=self,
            )
        except BaseException:
            # 如果 AgentContext 或 ReActAgent 创建失败，
            # except BaseException 会立即清除刚刚登记的取消事件，避免产生悬挂运行。
            self._task_service.clear_cancel_event(run_id)
            raise

    async def stop(self, *, session_id: str, user_id: str) -> bool:
        """校验会话归属，并取消该会话当前登记的全部运行。"""
        sid = SessionId(session_id)
        session = await self._session_manager.get_session(sid)
        if not session:
            raise NotFoundError("会话不存在")
        if session["user_id"] != user_id:
            raise ForbiddenError("无权访问")
        return self.cancel_session(session_id)

    def cancel_session(self, session_id: str) -> bool:
        """把传输层断连转换为既有会话级取消信号。"""
        return self._task_service.cancel_session(session_id)

    async def _finalize(self, run: ChatRun) -> None:
        """释放本次 run，再提交 Agent 已接管的非空完整答复；保存失败向上暴露。"""
        self._task_service.clear_cancel_event(run.run_id)
        result = run.agent.result
        if result and result.content.strip():
            content = result.content.strip()
            await self._session_manager.add_message(
                session_id=run.session_id,
                role="assistant",
                content=content,
                reasoning_content=result.reasoning or None,
                token_count=self._context_manager.count_tokens(content),
            )
