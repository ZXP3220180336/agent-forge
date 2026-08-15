"""app/config/settings.py Settings 配置校验与属性单元测试

注意：仓库根存在 .env（真实配置），因此测试一律用 `_make()`（_env_file=None）
跳过 .env 读取，只断言显式 kwargs 的确定性结果，避免环境耦合。
"""

import pytest
from pydantic import ValidationError

from app.config import settings
from app.config.settings import Settings, get_settings


def _make(**kwargs) -> Settings:
    """构造 Settings，跳过 .env 文件，避免仓库根 .env 干扰测试确定性。"""
    return Settings(_env_file=None, **kwargs)


# ===== 字段校验器 =====

VALIDATOR_CASES = [
    ("llm_temperature", [2.5, -0.1]),
    ("agent_max_iterations", [0, 101]),
    ("agent_max_concurrent_tasks", [0, 101]),
    ("agent_priority_queue_size", [0, 10001]),
    ("llm_embedding_dimensions", [0, -5]),
    ("jwt_expire_minutes", [0, 10081]),
    ("llm_reserve_safety_margin", [0.9, 4.1]),
    ("llm_reserve_quantile", [0, 1]),
    ("llm_reserve_reasoning_quantile", [0, 1]),
    ("llm_reserve_min_samples", [0]),
    ("llm_reserve_window", [0]),
]


@pytest.mark.parametrize("field,invalid_values", VALIDATOR_CASES)
def test_validators_reject_invalid(field, invalid_values):
    """每个校验器拒绝越界值"""
    for value in invalid_values:
        with pytest.raises(ValidationError):
            _make(**{field: value})


VALID_BOUNDARY_CASES = [
    ("llm_temperature", [0.0, 2.0]),
    ("agent_max_iterations", [1, 100]),
    ("agent_max_concurrent_tasks", [1, 100]),
    ("agent_priority_queue_size", [1, 10000]),
    ("llm_embedding_dimensions", [1]),
    ("jwt_expire_minutes", [1, 10080]),
    ("llm_reserve_safety_margin", [1.0, 4.0]),
    ("llm_reserve_quantile", [0.5]),
    ("llm_reserve_reasoning_quantile", [0.5]),
    ("llm_reserve_min_samples", [1]),
    ("llm_reserve_window", [1]),
]


@pytest.mark.parametrize("field,valid_values", VALID_BOUNDARY_CASES)
def test_validators_accept_boundaries(field, valid_values):
    """边界值合法且原样保留"""
    for value in valid_values:
        s = _make(**{field: value})
        assert getattr(s, field) == value


# ===== 属性 / 配置字典 =====


def test_is_production():
    assert _make(debug=False).is_production is True
    assert _make(debug=True).is_production is False


def test_llm_config():
    s = _make(
        llm_api_key="k",
        llm_base_url="u",
        llm_model_id="m",
        llm_temperature=0.3,
        llm_max_tokens=10,
        llm_timeout=5,
    )
    assert s.llm_config == {
        "api_key": "k",
        "base_url": "u",
        "model": "m",
        "temperature": 0.3,
        "max_tokens": 10,
        "timeout": 5,
    }


def test_llm_reasoning_config_falls_back_to_main():
    """推理模型未配置时回退到主模型"""
    s = _make(llm_model_id="main", llm_reasoning_model_id="")
    assert s.llm_reasoning_config["model"] == "main"


def test_llm_reasoning_config_uses_reasoning_model():
    s = _make(llm_model_id="main", llm_reasoning_model_id="deep")
    assert s.llm_reasoning_config["model"] == "deep"


def test_llm_fast_config_falls_back_to_main():
    s = _make(llm_model_id="main", llm_fast_model_id="")
    assert s.llm_fast_config["model"] == "main"


def test_llm_fast_config_uses_fast_model():
    s = _make(llm_model_id="main", llm_fast_model_id="quick")
    assert s.llm_fast_config["model"] == "quick"


def test_llm_embedding_config():
    s = _make(
        llm_api_key="k",
        llm_base_url="u",
        llm_embedding_model_id="emb",
        llm_embedding_dimensions=256,
    )
    assert s.llm_embedding_config == {
        "api_key": "k",
        "base_url": "u",
        "model": "emb",
        "dimensions": 256,
    }


def test_agent_config():
    s = _make(
        agent_max_iterations=5,
        agent_timeout=10,
        agent_streaming=False,
        agent_priority_levels=["low", "normal"],
        agent_default_priority="low",
        agent_high_priority_timeout=60,
        agent_low_priority_timeout=30,
        agent_priority_queue_size=20,
    )
    assert s.agent_config == {
        "max_iterations": 5,
        "timeout": 10,
        "streaming": False,
        "priority_levels": ["low", "normal"],
        "default_priority": "low",
        "high_priority_timeout": 60,
        "low_priority_timeout": 30,
        "priority_queue_size": 20,
    }


def test_concurrency_config():
    s = _make(
        agent_max_concurrent_tasks=2,
        agent_max_concurrent_tools=1,
        agent_task_queue_size=3,
        agent_worker_pool_size=4,
    )
    assert s.concurrency_config == {
        "max_concurrent_tasks": 2,
        "max_concurrent_tools": 1,
        "task_queue_size": 3,
        "worker_pool_size": 4,
    }


def test_database_config():
    s = _make(database_url="u", database_pool_size=1, database_max_overflow=2, database_echo=True)
    assert s.database_config == {"url": "u", "pool_size": 1, "max_overflow": 2, "echo": True}


def test_redis_config():
    s = _make(redis_url="r", redis_session_ttl=123)
    assert s.redis_config == {"url": "r", "session_ttl": 123}


def test_memory_config():
    s = _make(
        memory_enabled=True,
        memory_max_short_term=5,
        memory_vector_db="qdrant",
        memory_collection="c",
    )
    assert s.memory_config == {
        "enabled": True,
        "max_short_term": 5,
        "vector_db": "qdrant",
        "collection": "c",
    }


def test_tool_config():
    s = _make(
        tool_timeout=1,
        tool_max_retries=2,
        tool_max_output_length=3,
        tool_max_content_length=4,
    )
    assert s.tool_config == {
        "timeout": 1,
        "max_retries": 2,
        "max_output_length": 3,
        "max_content_length": 4,
    }


def test_extra_allow():
    """extra=allow：未知字段原样保留"""
    s = _make(unknown_key="v")
    assert s.unknown_key == "v"


# ===== 环境变量 / 单例 =====


def test_env_var_override(monkeypatch):
    """环境变量优先于 .env 文件"""
    monkeypatch.setenv("llm_model_id", "from-env")
    assert Settings().llm_model_id == "from-env"


def test_env_var_case_insensitive(monkeypatch):
    """环境变量大小写不敏感，DEBUG 映射到 debug"""
    monkeypatch.setenv("DEBUG", "true")
    assert Settings().debug is True


def test_singleton_identity():
    """get_settings() 经 lru_cache 返回同一实例"""
    assert get_settings() is settings
    assert isinstance(settings, Settings)
