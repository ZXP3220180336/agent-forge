"""数据库驱动依赖验收：真实加载驱动和构造引擎，不连接数据库。"""

from importlib import import_module

from sqlalchemy.ext.asyncio import create_async_engine


def test_asyncpg_driver_is_importable() -> None:
    """当前 Python 环境必须能真实导入正式 PostgreSQL 驱动。"""
    driver = import_module("asyncpg")
    assert callable(driver.connect)


async def test_postgresql_async_engine_can_load_driver() -> None:
    """SQLAlchemy 能加载 asyncpg 方言；构造成功不作为数据库就绪证据。"""
    engine = create_async_engine("postgresql+asyncpg://localhost/database_dependency_test")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "asyncpg"
    finally:
        await engine.dispose()
