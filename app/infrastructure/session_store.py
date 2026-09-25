"""PostgreSQL 会话适配器：每次操作独占 Session，不重试事务。"""

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError, DisconnectionError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import DatabaseRuntimeError
from app.infrastructure.models.database import MessageModel, SessionModel
from app.shared.exceptions import PersistenceUnavailableError
from app.shared.types import SessionId, UserId


def _unavailable_reason(error: BaseException) -> str | None:
    """仅分类已知设施故障；操作与清理共享相同白名单。"""
    if isinstance(error, (TimeoutError, PoolTimeoutError)):
        return "timeout"
    if isinstance(error, DatabaseRuntimeError):
        return "unavailable"
    if isinstance(error, (DisconnectionError, OSError)):
        return "connection_failed"
    if not isinstance(error, DBAPIError):
        return None
    sqlstate = getattr(error.orig, "sqlstate", None)
    if sqlstate == "42501":
        return "permission_denied"
    if sqlstate == "42P01":
        return "schema_missing"
    if sqlstate in {"42703", "3F000"}:
        return "schema_mismatch"
    if sqlstate in {"57014", "55P03"}:
        return "timeout"
    if error.connection_invalidated or (
        isinstance(sqlstate, str) and (sqlstate.startswith(("08", "28")) or sqlstate in {"57P01", "57P02", "57P03"})
    ):
        return "connection_failed"
    return None


class _SettlementFacts(Protocol):
    """异常携带的结算事实：提交成功已确认，或提交已开始但未收到确认。"""

    committed: bool
    commit_unknown: bool


def _mark_settlement(error: BaseException, *, committed: bool, commit_unknown: bool) -> None:
    """就地把结算事实挂到任意异常上，使其向上传播时不被抹除。"""
    facts = cast(_SettlementFacts, error)
    facts.committed = committed
    facts.commit_unknown = commit_unknown


class PostgresSessionStore:
    """事务与清理只有 worker 一个 Owner；未退出 worker 保留登记并禁止新准入。"""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] | None,
        *,
        operation_timeout_seconds: float,
        cleanup_timeout_seconds: float,
        is_available: Callable[[], bool],
    ):
        for value in (operation_timeout_seconds, cleanup_timeout_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Store timeout must be finite and positive")
        self._factory = session_factory
        self._operation_timeout = operation_timeout_seconds
        self._cleanup_timeout = cleanup_timeout_seconds
        self._is_available = is_available
        self._pending: set[asyncio.Task] = set()
        self._cleanup_failed = False

    @property
    def pending_count(self) -> int:
        """包括迟到收尾的任务数；runtime shutdown 仍负责自己的 Session。"""
        return len(self._pending)

    def ensure_available(self) -> None:
        self._require_factory()

    def _require_factory(self) -> Callable[[], AsyncSession]:
        """准入判据的唯一实现；返回非空工厂，同时为 `_run` 收窄类型。"""
        factory = self._factory
        if factory is None or self._cleanup_failed or not self._is_available():
            raise PersistenceUnavailableError()
        return factory

    async def _run(self, operation, *, write: bool = False):
        factory = self._require_factory()
        loop = asyncio.get_running_loop()
        end = loop.time() + self._operation_timeout
        work_end = end - min(self._cleanup_timeout, self._operation_timeout / 2)
        stopped = False
        commit_started = False
        committed = False
        primary_error = None

        def guard():
            if stopped or loop.time() >= work_end:
                raise TimeoutError()

        async def worker():
            nonlocal commit_started, committed, primary_error
            db = None
            failure = None
            try:
                guard()
                db = factory()

                async def execute(stmt):
                    guard()
                    result = await db.execute(stmt)
                    guard()
                    return result

                result = await operation(execute)
                guard()
                if write:
                    commit_started = True
                    await db.commit()
                    committed = True
                    guard()
                return result
            except BaseException as error:
                _mark_settlement(error, committed=committed, commit_unknown=commit_started and not committed)
                failure = error
                primary_error = error
                raise
            finally:
                if db is not None:
                    # 不另建 close task，避免 rollback 与仍在执行的 SQL 争用同一 Session。
                    try:
                        cleanup_end = min(end, loop.time() + self._cleanup_timeout)
                        async with asyncio.timeout_at(cleanup_end):
                            if not committed:
                                await db.rollback()
                            await db.close()
                            if loop.time() >= cleanup_end:
                                raise TimeoutError()
                    except BaseException as cleanup_error:
                        self._cleanup_failed = True
                        if failure is None:
                            if _unavailable_reason(cleanup_error) is None and not isinstance(
                                cleanup_error, asyncio.CancelledError
                            ):
                                _mark_settlement(
                                    cleanup_error,
                                    committed=committed,
                                    commit_unknown=commit_started and not committed,
                                )
                                primary_error = cleanup_error
                                raise
                            raise PersistenceUnavailableError(
                                "cleanup_incomplete",
                                committed=committed,
                                commit_unknown=commit_started and not committed,
                            ) from None

        def settled(task):
            self._pending.discard(task)
            if not task.cancelled():
                task.exception()  # 收走迟到异常，绝不记录 SQL/密码或再次执行。

        task = asyncio.create_task(worker())
        self._pending.add(task)
        task.add_done_callback(settled)

        async def stop_and_wait():
            nonlocal stopped
            stopped = True
            task.cancel()
            remaining = max(0, end - loop.time())
            if remaining:
                await asyncio.wait({task}, timeout=remaining)
            if not task.done():
                self._cleanup_failed = True

        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, work_end - loop.time()))
            if not done:
                await stop_and_wait()
                if primary_error is not None and not isinstance(primary_error, (asyncio.CancelledError, TimeoutError)):
                    raise primary_error
                raise PersistenceUnavailableError(
                    "commit_unknown" if commit_started and not committed else "timeout",
                    commit_unknown=commit_started and not committed,
                    committed=committed,
                )
            result = task.result()
            if loop.time() >= end:
                raise PersistenceUnavailableError(
                    "timeout", committed=committed, commit_unknown=commit_started and not committed
                )
            return result
        except asyncio.CancelledError as error:
            try:
                await stop_and_wait()
            except asyncio.CancelledError:
                self._cleanup_failed = True
            _mark_settlement(error, committed=committed, commit_unknown=commit_started and not committed)
            raise
        except (DBAPIError, DisconnectionError, PoolTimeoutError, OSError, TimeoutError, DatabaseRuntimeError) as error:
            unknown = commit_started and not committed
            reason = _unavailable_reason(error)
            if reason is None:
                # 约束冲突、SQL 编程错误等不能伪装成可恢复的不可用。
                raise
            raise PersistenceUnavailableError(
                "commit_unknown" if unknown else reason, commit_unknown=unknown, committed=committed
            ) from None

    async def create_session(self, *, session_id: SessionId, user_id: UserId, system_prompt: str, title: str) -> dict:
        """写入并提交会话，返回持久化使用的创建时间及零统计。"""
        created_at = datetime.now(UTC)

        async def operation(execute):
            await execute(
                insert(SessionModel).values(
                    id=session_id, user_id=user_id, system_prompt=system_prompt, title=title, created_at=created_at
                )
            )
            return {
                "id": session_id,
                "user_id": user_id,
                "system_prompt": system_prompt,
                "created_at": created_at.isoformat(),
                "message_count": 0,
                "total_tokens": 0,
            }

        return await self._run(operation, write=True)

    async def get_session(self, session_id: SessionId) -> dict | None:
        """按 ID 读取基本信息；不存在返回 None。"""

        async def operation(execute):
            session = (await execute(select(SessionModel).where(SessionModel.id == session_id))).scalar_one_or_none()
            if session is None:
                return None
            return {
                "id": session.id,
                "user_id": session.user_id,
                "system_prompt": session.system_prompt,
                "created_at": session.created_at.isoformat(),
                "message_count": 0,
                "total_tokens": 0,
            }

        return await self._run(operation)

    async def get_messages(
        self, session_id: SessionId, limit: int = 50, offset: int = 0, before_message_id: int | None = None
    ) -> list[dict]:
        """获取最新窗口后恢复正序，消息 ID 上界为排他条件。"""

        async def operation(execute):
            conditions = [MessageModel.session_id == session_id, MessageModel.role.in_(["user", "assistant"])]
            if before_message_id is not None:
                conditions.append(MessageModel.id < before_message_id)
            result = await execute(
                select(MessageModel)
                .where(*conditions)
                .order_by(MessageModel.created_at.desc(), MessageModel.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [{"role": msg.role, "content": msg.content} for msg in reversed(result.scalars().all())]

        return await self._run(operation)

    async def add_message(
        self, session_id: SessionId, role: str, content: str, reasoning_content: str | None = None, token_count: int = 0
    ) -> int:
        """插入消息并返回主键；无有效主键时回滚，不自动重试。"""

        async def operation(execute):
            result = await execute(
                insert(MessageModel).values(
                    session_id=session_id,
                    role=role,
                    content=content,
                    reasoning_content=reasoning_content,
                    token_count=token_count,
                )
            )
            key = result.inserted_primary_key
            if not key or isinstance(key[0], bool) or not isinstance(key[0], int) or key[0] <= 0:
                raise RuntimeError("数据库返回了无效消息主键")
            return key[0]

        return await self._run(operation, write=True)

    async def delete_session(self, session_id: SessionId) -> None:
        """软删除会话并刷新更新时间。"""

        async def operation(execute):
            await execute(
                update(SessionModel)
                .where(SessionModel.id == session_id)
                .values(status="deleted", updated_at=datetime.now(UTC))
            )

        await self._run(operation, write=True)

    async def hard_delete_session(self, session_id: SessionId) -> None:
        """在同一事务先删消息再删会话。"""

        async def operation(execute):
            await execute(delete(MessageModel).where(MessageModel.session_id == session_id))
            await execute(delete(SessionModel).where(SessionModel.id == session_id))

        await self._run(operation, write=True)

    @staticmethod
    def _item(session) -> dict:
        return {
            "id": session.id,
            "title": session.title,
            "system_prompt": session.system_prompt,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
            "status": session.status,
        }

    async def list_sessions(self, user_id: UserId, limit: int = 20, offset: int = 0) -> list[dict]:
        """读取活跃会话分页，不包含消息统计。"""

        async def operation(execute):
            result = await execute(
                select(SessionModel)
                .where(SessionModel.user_id == user_id, SessionModel.status == "active")
                .order_by(SessionModel.updated_at.desc().nullslast())
                .offset(offset)
                .limit(limit)
            )
            return [self._item(session) for session in result.scalars().all()]

        return await self._run(operation)

    async def get_session_stats(self, session_id: SessionId) -> dict:
        """聚合 user/assistant 消息数、token 和最后消息时间。"""

        async def operation(execute):
            row = (
                await execute(
                    select(
                        func.count(MessageModel.id).label("message_count"),
                        func.coalesce(func.sum(MessageModel.token_count), 0).label("total_tokens"),
                        func.max(MessageModel.created_at).label("last_message_at"),
                    ).where(MessageModel.session_id == session_id, MessageModel.role.in_(["user", "assistant"]))
                )
            ).one()
            return {
                "message_count": row.message_count,
                "total_tokens": row.total_tokens,
                "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
            }

        return await self._run(operation)

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
        """保持旧筛选排序语义，在同一事务读取总数和分页。"""

        async def operation(execute):
            conditions = [SessionModel.user_id == user_id]
            if status in ("active", "archived", "deleted"):
                conditions.append(SessionModel.status == status)
            elif not status:
                conditions.append(SessionModel.status != "deleted")
            if keyword:
                conditions.append(SessionModel.title.ilike(f"%{keyword}%"))
            if start_date:
                conditions.append(SessionModel.created_at >= start_date)
            if end_date:
                conditions.append(SessionModel.created_at <= end_date)
            column = getattr(SessionModel, sort_by, SessionModel.updated_at)
            order = column.desc().nullslast() if sort_order == "desc" else column.asc().nullsfirst()
            count = (await execute(select(func.count(SessionModel.id)).where(*conditions))).scalar() or 0
            result = await execute(select(SessionModel).where(*conditions).order_by(order).offset(offset).limit(limit))
            return [self._item(session) for session in result.scalars().all()], count

        return await self._run(operation)
