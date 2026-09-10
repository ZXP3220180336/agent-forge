"""API 请求体边界契约测试。"""

import pytest
from pydantic import ValidationError

from app.api.schemas.request import SendMessageRequest


def test_send_message_max_iterations_uses_runtime_default_when_omitted():
    """请求未覆盖迭代数时保留 None，由路由采用装配根配置。"""
    request = SendMessageRequest(session_id="s", message="m")
    assert request.max_iterations is None


@pytest.mark.parametrize("value", [1, 100])
def test_send_message_max_iterations_accepts_supported_boundaries(value):
    request = SendMessageRequest(session_id="s", message="m", max_iterations=value)
    assert request.max_iterations == value


@pytest.mark.parametrize("value", [0, -1, 101])
def test_send_message_max_iterations_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        SendMessageRequest(session_id="s", message="m", max_iterations=value)
