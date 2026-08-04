# ============================================
# routers/chat_router.py - 聊天相关 API 路由
# ============================================

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core.agent import AgentContext, ReActAgent
from app.core.events import build_error_event
from app.dependencies import (
    get_context_manager,
    get_current_user,
    get_llm_service,
    get_session_manager,
    get_task_service,
    get_tool_service,
)
from app.models.schemas.request import SendMessageRequest
from app.services import (
    ContextManager,
    LLMService,
    SessionManager,
    TaskService,
    ToolService,
)

router = APIRouter(prefix="/api", tags=["聊天"])


@router.post("/chat/send")
async def send_message(
    request: SendMessageRequest,
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
    context_manager: ContextManager = Depends(get_context_manager),  # noqa: B008
    llm_service: LLMService = Depends(get_llm_service),  # noqa: B008
    tool_service: ToolService = Depends(get_tool_service),  # noqa: B008
    task_service: TaskService = Depends(get_task_service),  # noqa: B008
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
    session = await session_manager.get_session(request.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")

    # 2. 保存用户消息
    await session_manager.add_message(
        session_id=request.session_id,
        role="user",
        content=request.message,
        token_count=context_manager.count_tokens(request.message),
    )

    # 3. 构建上下文
    messages, _ = await context_manager.build_messages(
        session_id=request.session_id,
        user_message=request.message,
    )

    # 4. 定义流式生成器
    async def generate():
        # Agent 无状态：每次请求新建实例，上下文通过 AgentContext 传入
        ctx = AgentContext(
            session_id=request.session_id,
            user_id=user_id,
            max_iterations=request.max_iterations,
        )
        agent = ReActAgent(llm=llm_service, tools=tool_service)

        try:
            # 4. ReAct 闭环：LLM 思考 → 工具调用 → LLM 总结
            # 经 TaskService 在任务级并发信号量（agent_max_concurrent_tasks）保护下运行
            async for event in task_service.run_agent(
                user_input=request.message,
                messages=messages,
                context=ctx,
                agent=agent,
            ):
                yield event

        except Exception as e:
            yield build_error_event(f"Agent 运行异常: {e!s}")
        finally:
            yield "data: [DONE]\n\n"

            # 5. 保存 AI 回复（流结束后从 agent.result 取最终答复）
            result = agent.result
            if result and result.content.strip():
                await session_manager.add_message(
                    session_id=request.session_id,
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
):
    """停止正在进行的聊天生成"""
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问")

    # 实际项目中，这里会调用 LLMService 的 cancel 方法
    return {"message": "已发送停止信号"}
