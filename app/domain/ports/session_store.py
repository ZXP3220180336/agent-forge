"""会话聚合专用持久化端口；只交换普通数据，不暴露数据库连接、ORM 或事务。"""

from datetime import datetime
from typing import Protocol

from app.shared.types import SessionId, UserId


class SessionStorePort(Protocol):
    """每个方法由适配器持有独立事务；硬删除两表属于同一操作，不自动重试。"""

    def ensure_available(self) -> None:
        """同步拒绝已知不可用状态；应用须在读取缓存之前调用。"""
        ...

    async def create_session(self, *, session_id: SessionId, user_id: UserId, system_prompt: str, title: str) -> dict:
        """提交新会话后返回基本信息及零计数，日期使用 ISO 字符串。"""
        ...

    async def get_session(self, session_id: SessionId) -> dict | None:
        """返回基本信息；不存在时返回 None。"""
        ...

    async def get_messages(
        self, session_id: SessionId, limit: int = 50, offset: int = 0, before_message_id: int | None = None
    ) -> list[dict]:
        """最新窗口恢复时间正序，仅 user/assistant，ID 上界排他。"""
        ...

    async def add_message(
        self, session_id: SessionId, role: str, content: str, reasoning_content: str | None = None, token_count: int = 0
    ) -> int:
        """提交消息并返回正整数主键；失败不得自动重放。"""
        ...

    async def delete_session(self, session_id: SessionId) -> None:
        """软删除，仅更新会话状态及更新时间。"""
        ...

    async def hard_delete_session(self, session_id: SessionId) -> None:
        """一个事务内先删消息再删会话。"""
        ...

    async def list_sessions(self, user_id: UserId, limit: int = 20, offset: int = 0) -> list[dict]:
        """活跃会话按 updated_at 降序/nulls last，返回不含统计的列表。"""
        ...

    async def get_session_stats(self, session_id: SessionId) -> dict:
        """统计 user/assistant 消息数量、token 总数及最后消息时间。"""
        ...

    async def list_sessions_v2(
        self,
        user_id: UserId,
        limit: int = 20,
        offset: int = 0,
        status: str | None = "active",
        keyword: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict], int]:
        """按现有搜索/过滤/排序语义返回不含统计的分页及总数。"""
        ...
