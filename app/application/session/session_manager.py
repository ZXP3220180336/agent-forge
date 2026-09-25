"""会话应用服务：生成会话 ID、应用默认值并维护 Redis 缓存。

数据库查询、事务及 ORM 映射由 SessionStorePort 适配器承担；端口返回时
数据库资源已经释放，缓存访问不会占用数据库会话。
"""

import json
import uuid
from datetime import datetime

import redis.asyncio as redis

from app.domain.ports.session_store import SessionStorePort
from app.platform.observability.logger import get_logger
from app.shared.exceptions import PersistenceUnavailableError
from app.shared.types import SessionId, UserId

logger = get_logger("services.session_manager")


class SessionManager:
    """使用 Redis 热缓存和会话持久化端口的应用服务。"""

    def __init__(self, redis_client: redis.Redis | None, store: SessionStorePort | None):
        self.redis = redis_client
        self.store = store
        self.session_ttl = 3600 * 24 * 7
        if self.redis is None:
            logger.warning("Redis 不可用，缓存降级")

    def _available_store(self) -> SessionStorePort:
        """缓存命中也不能掩盖已知的持久化不可用状态。"""
        store = self.store
        if store is None:
            raise PersistenceUnavailableError()
        store.ensure_available()
        return store

    async def _cache_get(self, key: str) -> bytes | str | None:
        if self.redis is None:
            return None
        return await self.redis.get(key)

    async def _cache_set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.redis is not None:
            await self.redis.set(key, value, ex)

    async def _cache_delete(self, key: str) -> None:
        if self.redis is not None:
            await self.redis.delete(key)

    async def create_session(
        self,
        user_id: UserId,
        system_prompt: str | None = None,
        title: str | None = None,
    ) -> dict:
        """创建会话，持久化成功后预热缓存。"""
        store = self._available_store()
        session_data = await store.create_session(
            session_id=SessionId(str(uuid.uuid4())),
            user_id=user_id,
            system_prompt=system_prompt or "你是一个友好的AI助手",
            title=title or "新对话",
        )
        await self._cache_set(f"session:{session_data['id']}", json.dumps(session_data), self.session_ttl)
        return session_data

    async def get_session(self, session_id: SessionId) -> dict | None:
        """优先读取缓存；缺失会话不缓存空值。"""
        store = self._available_store()
        cached = await self._cache_get(f"session:{session_id}")
        if cached:
            store.ensure_available()
            return json.loads(cached)
        session_data = await store.get_session(session_id)
        if session_data is not None:
            await self._cache_set(f"session:{session_id}", json.dumps(session_data), self.session_ttl)
        return session_data

    async def get_messages(
        self,
        session_id: SessionId,
        limit: int = 50,
        offset: int = 0,
        before_message_id: int | None = None,
    ) -> list[dict]:
        """获取最新 user/assistant 消息窗口，返回时间正序；消息 ID 上界排他。"""
        store = self._available_store()
        return await store.get_messages(session_id, limit=limit, offset=offset, before_message_id=before_message_id)

    async def add_message(
        self,
        session_id: SessionId,
        role: str,
        content: str,
        reasoning_content: str | None = None,
        token_count: int = 0,
    ) -> int:
        """持久化消息并返回端口已确认的正整数主键，不自动重试。"""
        store = self._available_store()
        return await store.add_message(
            session_id,
            role,
            content,
            reasoning_content=reasoning_content,
            token_count=token_count,
        )

    async def delete_session(self, session_id: SessionId) -> None:
        """软删除会话，保留先失效单会话缓存的既有顺序。"""
        store = self._available_store()
        await self._cache_delete(f"session:{session_id}")
        await store.delete_session(session_id)

    async def hard_delete_session(self, session_id: SessionId) -> None:
        """物理删除会话及消息；两表删除的事务由端口实现持有。"""
        store = self._available_store()
        await self._cache_delete(f"session:{session_id}")
        await store.hard_delete_session(session_id)

    async def list_sessions(
        self,
        user_id: UserId,
        limit: int = 20,
        offset: int = 0,
        include_stats: bool = True,
    ) -> list[dict]:
        """获取活跃会话及可选统计，沿用第一页 30 秒缓存。"""
        store = self._available_store()
        limit = min(max(1, limit), 100)
        offset = max(0, offset)
        cache_key = f"user_sessions:{user_id}:page:{offset // limit}"
        if offset == 0:
            cached = await self._cache_get(cache_key)
            if cached:
                store.ensure_available()
                return json.loads(cached)
        sessions = await store.list_sessions(user_id, limit=limit, offset=offset)
        session_list = []
        for session in sessions:
            item = dict(session)
            if include_stats:
                item.update(await self._get_session_stats(item["id"]))
            session_list.append(item)
        if offset == 0:
            await self._cache_set(cache_key, json.dumps(session_list), 30)
        return session_list

    async def _get_session_stats(self, session_id: SessionId) -> dict:
        """聚合统计沿用 60 秒缓存，不跨缓存持有数据库会话。"""
        store = self._available_store()
        cache_key = f"session_stats:{session_id}"
        cached = await self._cache_get(cache_key)
        if cached:
            store.ensure_available()
            return json.loads(cached)
        stats = await store.get_session_stats(session_id)
        await self._cache_set(cache_key, json.dumps(stats), 60)
        return stats

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
        include_stats: bool = True,
    ) -> tuple[list[dict], int]:
        """按搜索、筛选、排序条件查询分页，并在端口返回后合并可选统计。"""
        store = self._available_store()
        sessions, total = await store.list_sessions_v2(
            user_id,
            limit=min(max(1, limit), 100),
            offset=max(0, offset),
            status=status,
            keyword=keyword,
            start_date=start_date,
            end_date=end_date,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        session_list = []
        for session in sessions:
            item = dict(session)
            if include_stats:
                item.update(await self._get_session_stats(item["id"]))
            session_list.append(item)
        return session_list, total
