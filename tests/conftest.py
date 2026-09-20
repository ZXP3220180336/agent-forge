"""
pytest 共享配置。

全局 pytest 配置见 pyproject.toml 的 [tool.pytest.ini_options]。
各测试文件自行声明 fixture（跨文件复用的走本文件）。
"""

import pytest


@pytest.fixture
def agent_params() -> dict:
    """Agent 装配参数（形状同 container.agent_params）。

    新增护栏字段时只改这一处：路由与 e2e 测试都按此形状构造装配根配置。
    """
    return {
        "max_iterations": 5,
        "temperature": 0.2,
        "max_tokens": 4096,
        "max_execution_time": 300,
        "max_context_rounds": 8,
        "max_context_tokens": 128000,
        "max_empty_retries": 2,
        "max_llm_fail_retries": 2,
        "max_tool_protocol_retries": 2,
        "max_same_action_turns": 3,
        "batch_cleanup_grace": 1.0,
    }
