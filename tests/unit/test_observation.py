"""app/shared/observation.py 非关键观测隔离边界单元测试。"""

import asyncio
import logging

import pytest

from app.shared.observation import isolate_observation
from tests.observation_helpers import exploding_handler

_TEST_LOGGER = "app.test.observation"


def test_isolate_observation_swallows_handler_failure():
    """handler 抛错被隔离：调用方不受影响。"""
    logger = logging.getLogger(_TEST_LOGGER)
    with exploding_handler(_TEST_LOGGER) as handler:
        isolate_observation(lambda: logger.warning("观测"))

    assert handler.calls == 1


def test_isolate_observation_swallows_non_logging_failure():
    """非日志的观测调用同样被隔离。"""

    def boom() -> None:
        raise RuntimeError("observation failed")

    assert isolate_observation(boom) is None


def test_isolate_observation_propagates_cancelled_error():
    """BaseException 继续传播：隔离边界不改取消语义。"""

    def cancel() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        isolate_observation(cancel)


def test_isolate_observation_propagates_generator_exit():
    """GeneratorExit 同样属 BaseException，必须传播（生成器收尾路径依赖此行为）。"""

    def close_generator() -> None:
        raise GeneratorExit()

    with pytest.raises(GeneratorExit):
        isolate_observation(close_generator)


def test_isolate_observation_returns_none_on_success():
    assert isolate_observation(lambda: 42) is None
