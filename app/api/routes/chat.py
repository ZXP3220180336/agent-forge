# ============================================
# routes/chat.py - 聊天相关 API 路由
# ============================================

from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.deps import (
    get_chat_service,
    get_current_user,
)
from app.api.schemas.request import SendMessageRequest
from app.application.chat import ChatRun, ChatService
from app.shared.events import build_error_event

router = APIRouter(prefix="/api", tags=["聊天"])


class _ChatStreamingResponse(StreamingResponse):
    """在 ASGI 响应调用边界关闭 body iterator 和聊天运行。"""

    def __init__(self, content: Any, *, run: ChatRun, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._run = run

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self._run.closed:
                # 请求当前运行取消
                self._run.request_cancel()

            # `self.body_iterator`：就是父类 `StreamingResponse` 持有的异步迭代器（也就是我们传入的流式生成器）
            close = getattr(self.body_iterator, "aclose", None)
            try:
                if close is not None:
                    # 关闭 body iterator
                    await close()
            finally:
                # 关闭聊天运行
                await self._run.aclose()


@router.post("/chat/send")
async def send_message(
    request: SendMessageRequest,
    http_request: Request,  # FastAPI 注入原始 Request（被动断连检测）
    user_id: str = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),  # noqa: B008
):
    """
    准备聊天运行并以 SSE 返回 Agent 事件。

    流程：
    1. ChatService 在响应头前完成授权、用户消息、历史快照和运行登记。
    2. 路由消费 ChatRun 事件，并把 HTTP 断连转换为会话级取消。
    3. 普通运行异常转换为 SSE error；正常结束追加一次 [DONE]。
    4. 生成器或 ASGI 发送退出时关闭运行，由 ChatRun 清理登记并提交答复。
    """
    run = await chat_service.prepare_message(
        session_id=request.session_id,
        user_id=user_id,
        message=request.message,
        max_iterations=request.max_iterations,
    )

    async def generate():
        disconnected = False
        stream_ended = False
        try:
            try:
                async with aclosing(run.events()) as events:
                    async for event in events:
                        # HTTP 断连只在传输层识别；转换为 Application 的会话级取消，
                        # 继续排水(让内部 Agent、工具、usage 和运行登记按照既有生命周期完成收尾)
                        # 但不再向已断客户端推送。
                        if await http_request.is_disconnected():
                            if not disconnected:
                                disconnected = True
                                # 取消当前会话的所有运行，避免产生悬挂运行。
                                chat_service.cancel_session(request.session_id)
                            continue
                        yield event
            except Exception as error:  # noqa: BLE001
                if not disconnected:
                    yield build_error_event(f"Agent 运行异常: {error!s}")

            stream_ended = True
            if not disconnected:
                yield "data: [DONE]\n\n"
        finally:
            if not stream_ended:
                run.request_cancel()
            await run.aclose()

    return _ChatStreamingResponse(
        generate(),
        run=run,
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
    chat_service: ChatService = Depends(get_chat_service),  # noqa: B008
):
    """校验会话归属并取消该会话当前登记的全部聊天运行。"""
    cancelled = await chat_service.stop(session_id=session_id, user_id=user_id)
    return {"message": "已发送停止信号", "cancelled": cancelled}
