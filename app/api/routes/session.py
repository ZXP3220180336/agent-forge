# ============================================
# routers/session_router.py - 会话管理 API 路由
# ============================================


from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_current_user,
    get_session_manager,
)
from app.api.schemas.request import CreateSessionRequest
from app.api.schemas.response import CreateSessionResponse
from app.application.session.session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["会话管理"])


@router.post("/session/create", response_model=CreateSessionResponse)
async def create_session(
    request: CreateSessionRequest,
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
):
    """创建新会话"""
    session = await session_manager.create_session(
        user_id=user_id,
        system_prompt=request.system_prompt,
        title=request.title,
    )
    return CreateSessionResponse(
        session_id=session["id"],
        title=session.get("title", "新对话"),
        created_at=session["created_at"],
    )


@router.get("/session/{session_id}")
async def get_session(
    session_id: str,
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
):
    """获取会话详情"""
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问")
    return session


@router.get("/session/{session_id}/history")
async def get_history(
    session_id: str,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
):
    """获取会话历史"""
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问")

    messages = await session_manager.get_messages(
        session_id,
        limit=limit,
        offset=offset,
    )
    return {"session_id": session_id, "messages": messages}


@router.get("/sessions")
async def list_sessions(
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
):
    """获取用户的所有会话列表"""
    # 实际实现中，从数据库查询该用户的所有活跃会话
    sessions = await session_manager.list_sessions(user_id=user_id)
    return {"sessions": sessions}


@router.delete("/session/{session_id}")
async def delete_session(
    session_id: str,
    user_id: str = Depends(get_current_user),
    session_manager: SessionManager = Depends(get_session_manager),  # noqa: B008
):
    """删除会话"""
    session = await session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权访问")

    await session_manager.delete_session(session_id)
    return {"message": "会话已删除"}
