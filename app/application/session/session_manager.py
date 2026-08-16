"""
会话管理模块
- 负责会话的创建、查询、删除
- 会话与用户绑定，支持权限控制
- 会话有过期时间，自动清理

会话管理模块是整个多轮对话系统的入口与基石，它负责：
1、会话生命周期管理：创建、查询、删除
2、消息持久化：存储用户和 AI 的每一轮对话
3、缓存加速：通过 Redis 减少数据库查询压力
4、安全隔离：确保用户只能访问自己的会话
"""

import json
import uuid
from datetime import UTC, datetime

import redis.asyncio as redis

from app.platform.observability.logger import get_logger

logger = get_logger("services.session_manager")
from sqlalchemy import (
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models.database import MessageModel, SessionModel


class SessionManager:
    """
    会话管理器
    使用 Redis 作为热缓存，Database 作为持久化存储
    """

    def __init__(self, redis_client: redis.Redis | None, db_session_factory):
        self.redis = redis_client
        self.db_session = db_session_factory
        self.session_ttl = 3600 * 24 * 7  # 7天过期

        if self.redis is None:
            logger.warning("Redis 不可用，缓存降级")

    async def create_session(
        self,
        user_id: str,
        system_prompt: str | None = None,
        title: str | None = None,
    ) -> dict:
        """创建新会话"""
        session_id = str(uuid.uuid4())

        # 持久化到数据库
        async with self.db_session() as db:
            stmt = insert(SessionModel).values(
                id=session_id,
                user_id=user_id,
                system_prompt=system_prompt or "你是一个友好的AI助手",
                title=title or "新对话",
            )
            await db.execute(stmt)
            await db.commit()

        # 预热 Redis 缓存
        session_data = {
            "id": session_id,
            "user_id": user_id,
            "system_prompt": system_prompt or "你是一个友好的AI助手",
            "created_at": datetime.now(UTC).isoformat(),
            "message_count": 0,
            "total_tokens": 0,
        }
        await self.redis.set(
            f"session:{session_id}",
            json.dumps(session_data),
            self.session_ttl,
        )

        return session_data

    async def get_session(self, session_id: str) -> dict | None:
        """获取会话信息（Redis → DB 缓存穿透保护）"""
        # 1. 查 Redis
        cached = await self.redis.get(f"session:{session_id}")
        if cached:
            return json.loads(cached)

        # 2. 查数据库
        async with self.db_session() as db:
            result = await db.execute(
                select(SessionModel).where(SessionModel.id == session_id)
            )
            session = result.scalar_one_or_none()
            if not session:
                return None

            # 回写 Redis
            session_data = {
                "id": session.id,
                "user_id": session.user_id,
                "system_prompt": session.system_prompt,
                "created_at": session.created_at.isoformat(),
                "message_count": 0,  # 懒加载
                "total_tokens": 0,
            }
            await self.redis.set(
                f"session:{session_id}",
                json.dumps(session_data),
                self.session_ttl,
            )
            return session_data

    """
    # 增强版：缓存空值防止穿透攻击
    async def get_session(self, session_id, enable_bloom_filter=True):
        # 布隆过滤器快速过滤（可选）
        if enable_bloom_filter and not self.bloom_filter.might_contain(session_id):
            return None

        # 查 Redis
        cached = await self.redis.get(f"session:{session_id}")
        if cached:
            if cached == "NULL":  # 空值标记
                return None
            return json.loads(cached)

        # 查数据库
        session = await self._query_db(session_id)
        if not session:
            # 缓存空值，TTL 设置较短（如 60 秒）
            await self.redis.setex(f"session:{session_id}", 60, "NULL")
            return None

        # 回写缓存
        await self.redis.setex(f"session:{session_id}", self.session_ttl, json.dumps(session))
        return session

    """

    async def get_messages(
        self,
        session_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        获取会话历史消息
        支持分页，避免一次性加载过多
        """
        async with self.db_session() as db:
            # 只返回 user 和 assistant 的消息（不包含 system 和 reasoning）
            stmt = (
                select(MessageModel)
                .where(
                    MessageModel.session_id == session_id,
                    MessageModel.role.in_(["user", "assistant"]),
                )
                .order_by(MessageModel.created_at.asc())
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(stmt)
            messages = result.scalars().all()

            return [{"role": msg.role, "content": msg.content} for msg in messages]

    """
    # 注意：OFFSET 分页在数据量很大时（超过几十万行）性能会下降，因为数据库需要跳过 offset 行。
    # 对于超大规模数据，推荐使用游标分页（Cursor-based Pagination），游标分页（推荐用于大规模数据）
    async def get_messages_cursor(self, session_id, cursor=None, limit=50):
        stmt = select(MessageModel).where(
            MessageModel.session_id == session_id,
            MessageModel.role.in_(["user", "assistant"]),
        )
        if cursor:
            stmt = stmt.where(MessageModel.id > cursor)  # 基于 ID 的游标
        stmt = stmt.order_by(MessageModel.created_at.asc()).limit(limit)

        result = await db.execute(stmt)
        messages = result.scalars().all()

        next_cursor = messages[-1].id if len(messages) == limit else None
        return [{"role": msg.role, "content": msg.content} for msg in messages], next_cursor
    """

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        reasoning_content: str | None = None,
        token_count: int = 0,
    ) -> int:
        """添加消息记录，返回消息ID"""
        async with self.db_session() as db:
            stmt = insert(MessageModel).values(
                session_id=session_id,
                role=role,
                content=content,
                reasoning_content=reasoning_content,
                token_count=token_count,
            )
            result = await db.execute(stmt)
            await db.commit()
            return result.inserted_primary_key[0] if result.inserted_primary_key else 0

    """
    async def delete_session(self, session_id: str):
        # 删除会话（软删除）
        await self.redis.delete(f"session:{session_id}")
        async with self.db_session() as db:
            stmt = delete(SessionModel).where(SessionModel.id == session_id)
            await db.execute(stmt)
            await db.commit()
    """

    async def delete_session(self, session_id):
        """软删除会话（推荐）"""
        await self.redis.delete(f"session:{session_id}")
        async with self.db_session() as db:
            stmt = (
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(status="deleted", updated_at=datetime.now(UTC))
            )
            await db.execute(stmt)
            await db.commit()

    async def hard_delete_session(self, session_id):
        """物理删除（仅管理员/定时任务使用）"""
        await self.redis.delete(f"session:{session_id}")
        async with self.db_session() as db:
            # 先删除消息（外键约束）
            await db.execute(
                delete(MessageModel).where(MessageModel.session_id == session_id)
            )
            # 再删除会话
            await db.execute(delete(SessionModel).where(SessionModel.id == session_id))
            await db.commit()

    async def list_sessions(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
        include_stats: bool = True,
    ) -> list[dict]:
        """
        获取用户的所有会话列表。

        策略：
        1. 先从 Redis 缓存中尝试获取热点会话列表
        2. 缓存未命中时从数据库查询
        3. 分页查询，避免全表扫描
        4. 可选：附带消息数量和 Token 消耗统计

        Args:
            user_id: 用户ID
            limit: 每页数量，默认20，最大100
            offset: 偏移量，用于分页
            include_stats: 是否包含统计信息（消息数、Token数）

        Returns:
            会话列表，按 updated_at 降序排列
        """
        # 参数校验
        limit = min(max(1, limit), 100)  # 限制最大100条
        offset = max(0, offset)

        # 1. 尝试从缓存读取（仅限第一页热门数据）
        cache_key = f"user_sessions:{user_id}:page:{offset // limit}"
        if offset == 0:  # 仅缓存第一页
            cached = await self.redis.get(cache_key)
            if cached:
                return json.loads(cached)

        # 2. 从数据库查询
        async with self.db_session() as db:
            # 查询活跃会话，按更新时间降序
            stmt = (
                select(SessionModel)
                .where(
                    SessionModel.user_id == user_id,
                    SessionModel.status == "active",
                )
                .order_by(SessionModel.updated_at.desc().nullslast())
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(stmt)
            sessions = result.scalars().all()

            # 3. 构建返回数据
            session_list = []
            for session in sessions:
                item = {
                    "id": session.id,
                    "title": session.title,
                    "system_prompt": session.system_prompt,
                    "created_at": session.created_at.isoformat()
                    if session.created_at
                    else None,
                    "updated_at": session.updated_at.isoformat()
                    if session.updated_at
                    else None,
                    "status": session.status,
                }

                # 可选：填充统计信息
                if include_stats:
                    stats = await self._get_session_stats(session.id, db)
                    item.update(stats)

                session_list.append(item)

        # 4. 缓存第一页数据（TTL 短一些，因为列表频繁变化）
        if offset == 0:
            await self.redis.set(cache_key, json.dumps(session_list), 30)  # 30秒缓存

        return session_list

    async def _get_session_stats(
        self,
        session_id: str,
        db: AsyncSession,
    ) -> dict:
        """
        获取会话的统计信息（消息数、Token数）。

        优先从 Redis 缓存获取，避免频繁聚合查询。
        """
        cache_key = f"session_stats:{session_id}"

        # 1. 查缓存
        cached = await self.redis.get(cache_key)
        if cached:
            return json.loads(cached)

        # 2. 查数据库（聚合查询）

        stmt = select(
            func.count(MessageModel.id).label("message_count"),
            func.coalesce(func.sum(MessageModel.token_count), 0).label("total_tokens"),
            func.max(MessageModel.created_at).label("last_message_at"),
        ).where(
            MessageModel.session_id == session_id,
            MessageModel.role.in_(["user", "assistant"]),
        )
        result = await db.execute(stmt)
        row = result.one()

        stats = {
            "message_count": row.message_count,
            "total_tokens": row.total_tokens,
            "last_message_at": row.last_message_at.isoformat()
            if row.last_message_at
            else None,
        }

        # 3. 缓存统计信息（60秒过期）
        await self.redis.set(cache_key, json.dumps(stats), 60)

        return stats

    async def list_sessions_v2(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
        status: str | None = "active",  # active / archived / deleted / None(全部)
        keyword: str | None = None,  # 标题关键词搜索
        start_date: datetime | None = None,  # 起始日期
        end_date: datetime | None = None,  # 结束日期
        sort_by: str = "updated_at",  # created_at / updated_at / title
        sort_order: str = "desc",  # asc / desc
        include_stats: bool = True,
    ) -> tuple[list[dict], int]:
        """
        增强版：支持搜索、筛选、排序、统计总数。

        Returns:
            (session_list, total_count): 会话列表和符合条件的总数（用于分页）
        """
        limit = min(max(1, limit), 100)
        offset = max(0, offset)

        async with self.db_session() as db:
            # 构建基础查询条件
            conditions = [SessionModel.user_id == user_id]
            if status:
                if status == "active":
                    conditions.append(SessionModel.status == "active")
                elif status == "archived":
                    conditions.append(SessionModel.status == "archived")
                elif status == "deleted":
                    conditions.append(SessionModel.status == "deleted")
            else:
                # 查询所有状态（排除彻底删除的）
                conditions.append(SessionModel.status != "deleted")

            if keyword:
                conditions.append(SessionModel.title.ilike(f"%{keyword}%"))
            if start_date:
                conditions.append(SessionModel.created_at >= start_date)
            if end_date:
                conditions.append(SessionModel.created_at <= end_date)

            # 构建排序
            sort_column = getattr(SessionModel, sort_by, SessionModel.updated_at)
            if sort_order == "desc":
                order_by = sort_column.desc().nullslast()
            else:
                order_by = sort_column.asc().nullsfirst()

            # 查询总数（用于分页）
            count_stmt = select(func.count(SessionModel.id)).where(*conditions)
            total_result = await db.execute(count_stmt)
            total_count = total_result.scalar() or 0

            # 查询分页数据
            stmt = (
                select(SessionModel)
                .where(*conditions)
                .order_by(order_by)
                .offset(offset)
                .limit(limit)
            )
            result = await db.execute(stmt)
            sessions = result.scalars().all()

            # 构建返回数据
            session_list = []
            for session in sessions:
                item = {
                    "id": session.id,
                    "title": session.title,
                    "system_prompt": session.system_prompt,
                    "created_at": session.created_at.isoformat()
                    if session.created_at
                    else None,
                    "updated_at": session.updated_at.isoformat()
                    if session.updated_at
                    else None,
                    "status": session.status,
                }

                if include_stats:
                    stats = await self._get_session_stats(session.id, db)
                    item.update(stats)

                session_list.append(item)

            return session_list, total_count
