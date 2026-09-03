# ============================================
# routes/chat.py - 聊天相关 API 路由
# ============================================


from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_agent_params,
    get_context_manager,
    get_cost_limiter,
    get_current_user,
    get_llm_service,
    get_session_manager,
    get_task_service,
    get_tool_service,
)
from app.api.schemas.request import SendMessageRequest
from app.application.context.context_manager import ContextManager
from app.application.session.session_manager import SessionManager
from app.application.task.task_service import TaskService
from app.domain.agent import AgentContext, ReActAgent
from app.domain.ports.cost_limiter import CostLimiterPort
from app.domain.ports.llm_gateway import LLMGateway
from app.domain.ports.tool_gateway import ToolGateway
from app.shared.events import build_error_event, build_info_event
from app.shared.exceptions import ForbiddenError, NotFoundError
from app.shared.types import SessionId, UserId

router = APIRouter(prefix="/api", tags=["聊天"])


@router.post("/chat/send")
async def send_message(
    request: SendMessageRequest,
    http_request: Request,  # FastAPI 注入原始 Request（被动断连检测）
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
    context_manager: ContextManager = Depends(get_context_manager),  # noqa: B008
    llm_service: LLMGateway = Depends(get_llm_service),  # noqa: B008
    tool_service: ToolGateway = Depends(get_tool_service),  # noqa: B008
    task_service: TaskService = Depends(get_task_service),  # noqa: B008
    agent_params: dict = Depends(get_agent_params),  # noqa: B008
    cost_limiter: CostLimiterPort | None = Depends(get_cost_limiter),  # noqa: B008
):
    """
    发送消息，流式返回 AI 回复

    流程：
    1. 会话验证与授权
    2. 保存用户消息到数据库
    3. 从上下文管理器构建 messages
    4. ReActAgent 闭环：LLM 思考 → 工具调用 → LLM 总结
    5. 逐事件推送 SSE
    6. 流结束后保存 assistant 回复
    """
    # 1. 会话验证与授权
    sid: SessionId = SessionId(request.session_id)
    uid: UserId = UserId(user_id)
    session = await session_manager.get_session(sid)
    if not session:
        raise NotFoundError("会话不存在")
    if session["user_id"] != user_id:
        raise ForbiddenError("无权访问该会话")

    # 2. 保存用户消息
    await session_manager.add_message(
        session_id=sid,
        role="user",
        content=request.message,
        token_count=context_manager.count_tokens(request.message),
    )

    # 3. 构建上下文
    messages, _total, truncated_history = await context_manager.build_messages(
        session_id=sid,
        user_message=request.message,
    )

    # 4. 定义流式生成器
    # 取消事件：请求开始时注册（/chat/stop 可能在任何时刻置位），流结束清理
    cancel_event = task_service.create_cancel_event(request.session_id)

    async def generate():
        # Agent 无状态：每次请求新建实例，上下文通过 AgentContext 传入
        ctx = AgentContext(
            session_id=sid,
            user_id=uid,
            temperature=agent_params["temperature"],
            max_tokens=agent_params["max_tokens"],
            max_iterations=request.max_iterations or agent_params["max_iterations"],
            max_execution_time=agent_params["max_execution_time"],
            max_context_rounds=agent_params["max_context_rounds"],
            max_context_tokens=agent_params["max_context_tokens"],
            max_empty_retries=agent_params["max_empty_retries"],
            max_llm_fail_retries=agent_params["max_llm_fail_retries"],
            max_same_action_turns=agent_params["max_same_action_turns"],
        )
        agent = ReActAgent(
            llm=llm_service,
            tools=tool_service,
            context_budget=context_manager,
            cost_limiter=cost_limiter,
            cancel_event=cancel_event,
        )

        # 上下文超限裁剪告警（流首显式提示，避免历史被静默丢弃无感知）
        if truncated_history:
            yield build_info_event(
                f"上下文超限，已裁剪最早 {truncated_history} 条历史消息以适配模型上下文窗口"
            )

        disconnected = False
        try:
            # 4. ReAct 闭环：LLM 思考 → 工具调用 → LLM 总结
            # 经 TaskService 在任务级并发信号量（agent_max_concurrent_tasks）保护下运行
            async for event in task_service.run_agent(
                user_input=request.message,
                messages=messages,
                context=ctx,
                agent=agent,
            ):
                # 客户端被动断连（关页/刷新/断网）：与 /chat/stop 同走优雅取消——置位
                # 会话取消事件让 Agent 在轮次边界收尾（不再发起新 LLM 调用/工具），并停止
                # 向已断客户端推送。仅在首次检测到断连时置位一次（后续排水轮次只消费不推送）。
                if await http_request.is_disconnected():
                    if not disconnected:
                        disconnected = True
                        task_service.cancel_session(request.session_id)
                    continue  # 不再推送，仅继续消费让 Agent 优雅走完（资源闭环）

                yield event

        except Exception as e:  # noqa: BLE001
            if not disconnected:
                yield build_error_event(f"Agent 运行异常: {e!s}")
        finally:
            if not disconnected:
                yield "data: [DONE]\n\n"

            # 清理取消事件（会话运行结束）
            task_service.clear_cancel_event(request.session_id)

            # 5. 保存 AI 回复（流结束后从 agent.result 取最终答复）
            result = agent.result
            if result and result.content.strip():
                await session_manager.add_message(
                    session_id=sid,
                    role="assistant",
                    content=result.content.strip(),
                    reasoning_content=result.reasoning or None,
                    token_count=context_manager.count_tokens(result.content),
                )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-Id": request.session_id,
        },
    )


@router.post("/chat/stop")
async def stop_chat(
    session_id: str,
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
    task_service: TaskService = Depends(get_task_service),  # noqa: B008
):
    """停止正在进行的聊天生成"""
    sid: SessionId = SessionId(session_id)
    session = await session_manager.get_session(sid)
    if not session:
        raise NotFoundError("会话不存在")
    if session["user_id"] != user_id:
        raise ForbiddenError("无权访问")

    # 置位会话取消事件 → 运行中的 Agent 在轮次边界优雅停止（after_turn 语义，
    # 不硬中断：LLM 调用在整流层 chunk 边界响应、工具执行完成后取消生效）
    cancelled = task_service.cancel_session(session_id)
    return {"message": "已发送停止信号", "cancelled": cancelled}
