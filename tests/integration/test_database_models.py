"""真实 PostgreSQL 上比较 ORM 与首迁移，并验证客户端默认的逐次写入行为。"""

import asyncio

from sqlalchemy import insert, select, text, update

from app.infrastructure import database_schema as schema
from app.infrastructure.models.database import Base, MessageModel, SessionModel

from .test_database_migrations import upgrade


async def test_orm_ddl_matches_initial_schema_catalog(pg_engine):
    async with pg_engine.begin() as connection:
        await connection.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[SessionModel.__table__, MessageModel.__table__])
        )
        await schema.validate_session_tables(connection, asyncio.get_running_loop().time() + 10, baseline=True)
    result = await upgrade(pg_engine, baseline=True)
    assert result.reason == "ok" and result.confirmed == [1]


async def test_actual_writes_get_new_timestamps_and_independent_metadata(pg_engine):
    assert (await upgrade(pg_engine)).reason == "ok"
    async with pg_engine.begin() as connection:
        first = (
            await connection.execute(
                insert(SessionModel)
                .values(id="one", user_id="test")
                .returning(SessionModel.created_at, SessionModel.updated_at, SessionModel.meta)
            )
        ).one()
        message1 = (
            await connection.execute(
                insert(MessageModel)
                .values(role="user", content="one")
                .returning(MessageModel.created_at, MessageModel.meta)
            )
        ).one()
        await asyncio.sleep(0.01)
        second = (
            await connection.execute(
                insert(SessionModel)
                .values(id="two", user_id="test")
                .returning(SessionModel.created_at, SessionModel.meta)
            )
        ).one()
        message2 = (
            await connection.execute(
                insert(MessageModel)
                .values(role="user", content="two")
                .returning(MessageModel.created_at, MessageModel.meta)
            )
        ).one()
        assert second.created_at > first.created_at
        assert message2.created_at > message1.created_at
        assert first.updated_at is None and first.meta == second.meta == message1.meta == message2.meta == {}
        updated = (
            await connection.execute(
                update(SessionModel)
                .where(SessionModel.id == "one")
                .values(title="changed")
                .returning(SessionModel.updated_at)
            )
        ).scalar_one()
        assert updated > second.created_at
        await connection.execute(update(SessionModel).where(SessionModel.id == "one").values(meta={"changed": True}))
        assert (await connection.execute(select(SessionModel.meta).where(SessionModel.id == "two"))).scalar_one() == {}


async def test_direct_sql_has_no_orm_defaults(pg_engine):
    assert (await upgrade(pg_engine)).reason == "ok"
    async with pg_engine.begin() as connection:
        row = (
            await connection.execute(
                text(
                    "INSERT INTO public.sessions(id,user_id) VALUES ('direct','user') "
                    "RETURNING title,system_prompt,created_at,updated_at,status,meta"
                )
            )
        ).one()
        assert all(value is None for value in row)
        row = (
            await connection.execute(
                text(
                    "INSERT INTO public.messages(role,content) VALUES ('user','direct') "
                    "RETURNING id,token_count,created_at,meta"
                )
            )
        ).one()
        assert row.id == 1 and tuple(row)[1:] == (None, None, None)


async def test_orm_always_uses_verified_public_tables(pg_engine):
    assert (await upgrade(pg_engine)).reason == "ok"
    async with pg_engine.begin() as connection:
        await connection.execute(text("CREATE TEMP TABLE sessions (LIKE public.sessions INCLUDING ALL) ON COMMIT DROP"))
        await connection.execute(text("SET LOCAL search_path=pg_temp,public"))
        await connection.execute(insert(SessionModel).values(id="shadow", user_id="test"))
        count = (await connection.execute(text("SELECT count(*) FROM public.sessions WHERE id='shadow'"))).scalar_one()
        assert count == 1
