# ============================================
# models/session.py - 会话数据库模型
# ============================================

from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, String, Text

from .base import Base


# ===== 数据库模型 =====
class SessionModel(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True)  # UUID
    user_id = Column(String(64), nullable=False, index=True)
    title = Column(String(200), default="新对话")
    system_prompt = Column(Text, default="你是一个友好的AI助手")
    created_at = Column(DateTime(timezone=True), default=datetime.now(UTC))
    updated_at = Column(DateTime(timezone=True), onupdate=datetime.now(UTC))
    status = Column(String(20), default="active")  # active, archived, deleted
    meta = Column(JSON, default={})  # 扩展字段
