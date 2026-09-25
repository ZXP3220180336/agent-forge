"""隔离 PostgreSQL 集成测试配置；仅消费显式授权的本地测试连接。"""

from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def pg_engine():
    """清理仅作用于三张受管表；发现其他 public 对象立即停止。"""
    config = dotenv_values(ROOT / ".env.test")
    if not config.get("DATABASE_URL"):
        pytest.skip("未配置隔离 PostgreSQL，真实验收未执行")
    url = make_url(config["DATABASE_URL"])
    assert config.get("DATABASE_TEST_ALLOW_CLEANUP") == "true", "必须明确授权测试清理"
    assert url.database == "agent_forge_test" and url.username == "agent_forge_test"
    engine = create_async_engine(url, hide_parameters=True, connect_args={"timeout": 5, "command_timeout": 10})

    async def cleanup():
        async with engine.begin() as connection:
            names = set(
                (
                    await connection.execute(
                        text(
                            "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                            "ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind NOT IN ('i','t','I')"
                        )
                    )
                ).scalars()
            )
            assert names <= {"sessions", "messages", "schema_versions", "messages_id_seq"}, "存在非测试对象，拒绝清理"
            for name in ("messages", "sessions", "schema_versions"):
                await connection.execute(text(f"DROP TABLE IF EXISTS public.{name}"))

    try:
        await cleanup()
        yield engine
    finally:
        await cleanup()
        await engine.dispose()
