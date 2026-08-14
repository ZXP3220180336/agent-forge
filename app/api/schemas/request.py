"""
请求体 Schema — 路由入参的数据契约。

集中管理所有 API 请求体模型，路由层只 import 使用，不在路由内定义。
"""

from pydantic import BaseModel


class SendMessageRequest(BaseModel):
    """聊天发送消息请求体。"""

    session_id: str
    message: str
    max_iterations: int = 10
    stream: bool = True  # 是否流式返回


class CreateSessionRequest(BaseModel):
    """创建会话请求体。"""

    system_prompt: str | None = None
    title: str | None = None
