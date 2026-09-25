# ============================================
# models/message.py - 消息数据库模型
# ============================================

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)

from .base import Base


class MessageModel(Base):
    __tablename__ = "messages"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("public.sessions.id"), index=True)
    role = Column(String(20), nullable=False)  # system, user, assistant
    content = Column(Text, nullable=False)
    reasoning_content = Column(Text, nullable=True)  # 思考过程（不进入历史）
    token_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    meta = Column(JSON, default=dict)
