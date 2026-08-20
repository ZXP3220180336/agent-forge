"""app/container.py Container 装配根单元测试

initialize() 只 stub 掉外部基础设施（Redis 连接、asyncpg 引擎、setup_logging），
其余装配路径（register_config / LLMService / 工具注册 / TaskService / EmbeddingService）
均为离线安全，真实执行。测试后会恢复被 initialize() 污染的全局注册表。
"""

import pytest

import app.container as container_module
from app.config import settings
from app.container import Container
from app.integration.llm import (
    ClientManager,
    ReservationLimiterManager,
    RetryHandlerManager,
    StreamingRectifier,
    StructuredOutput,
)
from app.integration.llm.llm_service import LLMService

# initialize() 会修改这些类级注册表/配置，测试后恢复为快照
_GLOBAL_STATE = {
    ClientManager: ("_instances", "_configs", "_pending_closes", "_closing_tasks"),
    RetryHandlerManager: ("_instances", "_config", "_circuit_breaker_config"),
    ReservationLimiterManager: ("_instances", "_configs"),
    LLMService: ("_fallback_model_id", "_adaptive_reserve", "_stream_max_retries"),
    StructuredOutput: ("_default_max_tokens",),
    StreamingRectifier: ("_base_delay", "_max_delay", "_use_jitter"),
}


@pytest.fixture(autouse=True)
def _restore_global_registries():
    saved = {
        cls: {attr: getattr(cls, attr) for attr in attrs}
        for cls, attrs in _GLOBAL_STATE.items()
    }
    yield
    for cls, attrs in _GLOBAL_STATE.items():
        for attr in attrs:
            setattr(cls, attr, saved[cls][attr])


class _FakeRedis:
    def __init__(self):
        self.closed = 0

    async def ping(self):
        return True

    async def close(self):
        self.closed += 1


class _FakeRedisClass:
    @classmethod
    def from_url(cls, *a, **k):
        return _FakeRedis()


class _FakeEngine:
    def __init__(self):
        self.disposed = 0

    async def dispose(self):
        self.disposed += 1


def _stub_infra(monkeypatch, fake_redis=None, fake_engine=None):
    """stub setup_logging + Redis.from_url + create_async_engine。"""
    monkeypatch.setattr(container_module, "setup_logging", lambda *a, **k: None)

    if fake_redis is None:
        fake_redis = _FakeRedis()

    class _RedisClass:
        @classmethod
        def from_url(cls, *a, **k):
            return fake_redis

    monkeypatch.setattr(container_module, "Redis", _RedisClass)

    if fake_engine is None:
        fake_engine = _FakeEngine()
    monkeypatch.setattr(container_module, "create_async_engine", lambda *a, **k: fake_engine)
    return fake_redis, fake_engine


def test_container_default_state():
    """默认状态：所有服务未初始化"""
    c = Container()
    assert c.redis is None
    assert c.db_session_factory is None
    assert c._engine is None
    assert c.session_manager is None
    assert c.context_manager is None
    assert c.llm_service is None
    assert c.tool_service is None
    assert c.task_service is None
    assert c.embedding_service is None
    assert c.agent_params == {}
    assert c.initialized is False
    assert c._errors == []


@pytest.mark.asyncio
async def test_initialize_happy_path(monkeypatch):
    """基础设施 stub 后，initialize 完整装配全部服务"""
    fake_redis, _ = _stub_infra(monkeypatch)
    c = Container()

    await c.initialize()

    assert c.initialized is True
    assert c.redis is fake_redis
    assert c.db_session_factory is not None
    assert c.session_manager is not None
    assert c.session_manager.redis is fake_redis
    assert c.context_manager is not None
    assert c.context_manager.model_name == settings.llm_model_id
    assert c.llm_service is not None
    assert c.tool_service is not None
    # 10 个内置工具（5 通用 + 5 RCA）+ external/ 示例 http_api（冷启动扫描注册，外部工具对 LLM 可见）
    assert len(c.tool_service.list_tools()) == 11
    assert "http_api" in c.tool_service.list_tools()
    assert c.task_service is not None
    assert c.embedding_service is not None
    assert c.agent_params == {
        "max_iterations": settings.agent_max_iterations,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }
    assert c._errors == []


@pytest.mark.asyncio
async def test_initialize_redis_failure_degrades(monkeypatch):
    """Redis 连接失败：降级为 None，但其余服务照常装配"""

    class _BoomRedis:
        @staticmethod
        def from_url(*a, **k):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(container_module, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(container_module, "Redis", _BoomRedis)
    monkeypatch.setattr(container_module, "create_async_engine", lambda *a, **k: _FakeEngine())

    c = Container()
    await c.initialize()

    assert c.redis is None
    assert c._errors and "Redis 连接失败" in c._errors[0]
    assert c.initialized is True
    assert c.session_manager is not None
    assert c.session_manager.redis is None


@pytest.mark.asyncio
async def test_initialize_engine_failure_degrades(monkeypatch):
    """数据库引擎失败（asyncpg 缺失）：db_session_factory 降级为 None"""

    def _boom(*a, **k):
        raise RuntimeError("no db driver")

    monkeypatch.setattr(container_module, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(container_module, "Redis", _FakeRedisClass)
    monkeypatch.setattr(container_module, "create_async_engine", _boom)

    c = Container()
    await c.initialize()

    assert c._engine is None
    assert c.db_session_factory is None
    assert c.session_manager is not None
    assert c.session_manager.db_session is None
    assert c._errors and "数据库初始化失败" in c._errors[0]
    assert c.initialized is True


@pytest.mark.asyncio
async def test_shutdown_closes_resources(monkeypatch):
    """shutdown 关闭 redis 与 engine"""
    fake_redis, fake_engine = _stub_infra(monkeypatch)
    c = Container()
    await c.initialize()

    await c.shutdown()

    assert fake_redis.closed == 1
    assert fake_engine.disposed == 1
    assert c.initialized is False


@pytest.mark.asyncio
async def test_shutdown_safe_when_nothing_initialized():
    """未初始化时 shutdown 应安全无异常"""
    c = Container()
    await c.shutdown()
    assert c.initialized is False
