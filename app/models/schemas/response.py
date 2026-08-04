"""
响应体 Schema — 路由出参的数据契约。

集中管理所有 API 响应体模型，路由层只 import 使用，不在路由内定义。
"""

from pydantic import BaseModel


class CreateSessionResponse(BaseModel):
    """创建会话响应体。"""

    session_id: str
    title: str
    created_at: str
