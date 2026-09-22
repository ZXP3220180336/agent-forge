"""数据库配置的有限预算、环境注入与凭证保护。"""

import os

import pytest
from pydantic import ValidationError

from app.config.settings import Settings

TIMEOUT_FIELDS = (
    "database_connect_timeout_seconds",
    "database_pool_timeout_seconds",
    "database_operation_timeout_seconds",
    "database_probe_timeout_seconds",
    "database_migration_timeout_seconds",
    "database_migration_total_timeout_seconds",
    "database_cleanup_timeout_seconds",
    "database_shutdown_timeout_seconds",
)


@pytest.fixture(autouse=True)
def isolate_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主数据库配置，测试不读取真实凭证或连接真实服务。"""
    for key in os.environ:
        if key.lower().startswith("database_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("field", TIMEOUT_FIELDS)
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("-inf"), float("nan"), True])
def test_database_timeouts_reject_unbounded_values(field: str, value: object) -> None:
    """等待预算必须是有限正数，布尔值不能冒充秒数。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("field", TIMEOUT_FIELDS)
def test_database_timeout_environment_reaches_config(field: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """字符串环境变量解析为秒数，并进入同一个数据库配置出口。"""
    monkeypatch.setenv(field.upper(), "2.5")
    config = Settings(_env_file=None).database_config
    assert config[field.removeprefix("database_")] == 2.5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_pool_size", 0),
        ("database_pool_size", -1),
        ("database_pool_size", True),
        ("database_pool_size", 1.5),
        ("database_max_overflow", -1),
        ("database_max_overflow", True),
        ("database_max_overflow", 1.5),
    ],
)
def test_database_pool_rejects_unlimited_or_noninteger_capacity(field: str, value: object) -> None:
    """不允许 SQLAlchemy 的无界池/溢出模式，也不静默接受非整数容量。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_database_smallest_pool_and_timeout_are_allowed() -> None:
    """单连接无溢出、正小数等待预算是合法配置。"""
    settings = Settings(
        _env_file=None,
        database_pool_size=1,
        database_max_overflow=0,
        **dict.fromkeys(TIMEOUT_FIELDS, 0.01),
    )
    assert settings.database_config["pool_size"] == 1
    assert settings.database_config["max_overflow"] == 0
    assert all(settings.database_config[field.removeprefix("database_")] == 0.01 for field in TIMEOUT_FIELDS)


@pytest.mark.parametrize("url", ["", "not-a-url", "sqlite+aiosqlite:///test.db", "postgresql://localhost/db"])
def test_database_url_requires_asyncpg_postgresql(url: str) -> None:
    """本轮唯一后端为 PostgreSQL/asyncpg，拒绝缺失或错误的驱动配置。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=url)


def test_database_url_preserves_escaped_credentials_without_repr_leak() -> None:
    """合法凭证原样交给连接层，但 Settings 的常规诊断不展示连接串。"""
    url = "postgresql+asyncpg://probe:sentinel%40password@localhost/probe"
    settings = Settings(_env_file=None, database_url=url)
    assert settings.database_config["url"] == url
    assert "sentinel" not in repr(settings)
    assert url not in str(settings)


def test_invalid_database_url_diagnostic_hides_input() -> None:
    """非法连接串的校验异常文本不能回显原始凭证。"""
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, database_url="wrong://probe:credential-sentinel@localhost/db")
    assert "credential-sentinel" not in str(caught.value)
    assert "wrong://" not in str(caught.value)
