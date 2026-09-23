"""真实 SQLAlchemy Session 的关闭责任回归；不替代 PostgreSQL 验收。"""

import asyncio

import pytest
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import DatabaseRuntime, DatabaseStatus, DatabaseTimeouts


def ready_runtime() -> DatabaseRuntime:
    """仅为独立生命周期测试装配引擎；不声明数据库已接受真实探测。"""
    runtime = DatabaseRuntime(
        url="postgresql+asyncpg://localhost/lifecycle_review",
        pool_size=1,
        max_overflow=0,
        echo=False,
        timeouts=DatabaseTimeouts(
            connect_timeout_seconds=0.2,
            pool_timeout_seconds=0.2,
            operation_timeout_seconds=0.2,
            probe_timeout_seconds=0.2,
            cleanup_timeout_seconds=0.05,
            shutdown_timeout_seconds=0.2,
        ),
    )
    runtime._build_engine()
    runtime._status = DatabaseStatus("ready", None)
    return runtime


async def test_cancelled_session_close_retains_owner_until_explicit_close(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = ready_runtime()
    session = runtime.session_factory()
    entered = asyncio.Event()
    original_close = AsyncSession.close

    async def blocked_close(self) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AsyncSession, "close", blocked_close)
    closing = asyncio.create_task(session.close())
    try:
        await entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert (await runtime.dispose()).reason == "close_incomplete"
    finally:
        monkeypatch.setattr(AsyncSession, "close", original_close)
        await session.close()
        assert (await runtime.dispose()).state == "closed"


async def test_context_manager_keeps_session_owner_until_shielded_close_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ready_runtime()
    session = runtime.session_factory()
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    original_close = AsyncSession.close

    async def delayed_close(self) -> None:
        entered.set()
        await release.wait()
        await original_close(self)
        closed.set()

    async def use_session() -> None:
        async with session:
            pass

    monkeypatch.setattr(AsyncSession, "close", delayed_close)
    owner = asyncio.create_task(use_session())
    try:
        await entered.wait()
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert (await runtime.dispose()).reason == "close_incomplete"
        release.set()
        await closed.wait()
        assert (await runtime.dispose()).state == "closed"
    finally:
        release.set()
        await session.close()
        await runtime.dispose()


async def test_closed_session_cannot_reopen_after_releasing_owner() -> None:
    runtime = ready_runtime()
    session = runtime.session_factory()
    try:
        await session.aclose()
        with pytest.raises(InvalidRequestError, match="permanently closed"):
            await session.begin()
        assert (await runtime.dispose()).state == "closed"
    finally:
        await session.close()
        await runtime.dispose()
