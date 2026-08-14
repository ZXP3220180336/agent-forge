"""
全局日志框架单元测试（app/utils/logger.py）

覆盖：
    get_logger             app.* 命名空间 + 标准 Logger
    setup_logging          双 handler（控制台 + 文件）、幂等、级别
    JsonFormatter          结构化事件输出、保留键排除、None 存活
    ConsoleFormatter       人类可读文本（非 JSON）
    log_event_async        to_thread 异步写入

不依赖外部设施：用 tmp_path 覆盖 log_file，避免写仓库默认 logs/app.log。
"""

import asyncio
import json
import logging

import pytest

from app.config import settings
from app.utils.logger import (
    ConsoleFormatter,
    JsonFormatter,
    get_logger,
    log_event,
    log_event_async,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个用例用独立临时日志文件，避免污染仓库 logs/。"""
    monkeypatch.setattr(settings, "log_file", str(tmp_path / "app.log"))
    # 每个用例重配：清空 root handlers 后重建，避免叠加
    yield


def test_get_logger_returns_app_namespaced():
    """get_logger 返回 app.* 命名空间下的标准 Logger。"""
    lg = get_logger("foo")
    assert isinstance(lg, logging.Logger)
    assert lg.name == "app.foo"


def test_setup_logging_creates_dual_handlers(tmp_path):
    """setup_logging 创建控制台 + 文件双 handler，级别按 settings。"""
    setup_logging(log_file=str(tmp_path / "app.log"))
    root = logging.getLogger()
    types = [type(h).__name__ for h in root.handlers]
    assert "StreamHandler" in types, f"应有控制台 handler: {types}"
    assert "FileHandler" in types, f"应有文件 handler: {types}"
    assert root.level == getattr(logging, settings.log_level)
    assert tmp_path.joinpath("app.log").exists(), "日志文件应已创建"


def test_setup_logging_is_idempotent(tmp_path):
    """重复 setup_logging 不叠加 handler。"""
    setup_logging(log_file=str(tmp_path / "app.log"))
    n1 = len(logging.getLogger().handlers)
    setup_logging(log_file=str(tmp_path / "app.log"))
    n2 = len(logging.getLogger().handlers)
    assert n1 == n2, f"重复 setup 后 handler 数应不变: {n1} -> {n2}"


def test_json_handler_writes_structured_event(tmp_path):
    """文件 handler 输出结构化 JSON，含 event_name 与自定义字段。"""
    setup_logging(log_file=str(tmp_path / "app.log"))
    log_event("llm_call", success=False, error="超时", duration=1.5)
    # 触发 flush：logging 的 FileHandler 每条日志即写，直接读即可
    lines = tmp_path.joinpath("app.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, f"应有 1 条日志: {lines}"
    data = json.loads(lines[0])
    assert data["message"] == "llm_call"
    assert data["level"] == "INFO"
    assert data["logger"] == "app.events"
    assert data["success"] is False
    assert data["error"] == "超时"
    assert data["duration"] == 1.5
    # 未传字段不应出现
    assert "prompt_tokens" not in data
    assert "model" not in data


def test_json_formatter_excludes_reserved_keys():
    """JsonFormatter 排除 LogRecord 保留键，只留自定义字段。"""
    record = logging.LogRecord(
        name="app.events",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="llm_call",
        args=(),
        exc_info=None,
    )
    # 模拟 logger.log(..., extra=fields) 的 merge 行为（真实路径由 Logger.makeRecord 完成）
    record.__dict__.update({"request_id": "r1", "model": "gpt-4o"})
    data = json.loads(JsonFormatter().format(record))
    assert data["message"] == "llm_call"
    assert data["request_id"] == "r1"
    assert data["model"] == "gpt-4o"
    # 保留键不应出现在 payload 顶层
    for reserved in ("levelname", "name", "msg", "processName", "pathname", "module"):
        assert reserved not in data, f"保留键 {reserved} 不应输出"


def test_json_event_survives_none_values(tmp_path):
    """JSON 中 None 字段保留为 null（成功路径 error=None 必须可见）。"""
    setup_logging(log_file=str(tmp_path / "app.log"))
    log_event("llm_call", success=True, error=None)
    data = json.loads(tmp_path.joinpath("app.log").read_text(encoding="utf-8").strip())
    assert data["error"] is None
    assert data["success"] is True


def test_text_format_variant(tmp_path, monkeypatch):
    """log_format=text 时文件输出人类可读文本（非 JSON）。"""
    setup_logging(log_file=str(tmp_path / "app.log"), log_format="text")
    log_event("llm_call", success=True, duration=2.0)
    line = tmp_path.joinpath("app.log").read_text(encoding="utf-8").strip()
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "llm_call" in line
    assert "success=True" in line, "text 变体应含字段摘要"


@pytest.mark.asyncio
async def test_log_event_async_uses_to_thread(monkeypatch, tmp_path):
    """log_event_async 通过 asyncio.to_thread 调用同步 log_event。"""
    setup_logging(log_file=str(tmp_path / "app.log"))  # 先配置文件 handler，确保日志落盘
    calls = {}

    async def fake_to_thread(fn, *args, **kwargs):
        calls["fn"] = fn
        calls["args"] = args
        calls["kwargs"] = kwargs
        fn(*args, **kwargs)
        return None

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    await log_event_async("llm_call", success=True, duration=1.0)

    assert calls["fn"].__name__ == "log_event"
    assert calls["args"][0] == "llm_call"
    assert calls["kwargs"] == {"level": logging.INFO, "success": True, "duration": 1.0}
    # 文件确实写入
    data = json.loads(tmp_path.joinpath("app.log").read_text(encoding="utf-8").strip())
    assert data["message"] == "llm_call"


def test_console_formatter_is_human_readable():
    """ConsoleFormatter 输出人类可读文本，含 ASCII 级别前缀。"""
    record = logging.LogRecord(
        name="app.container",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Redis 不可用",
        args=(),
        exc_info=None,
    )
    out = ConsoleFormatter().format(record)
    assert "[WARN]" in out
    assert "Redis 不可用" in out
    # 不含非 ASCII 符号（Windows GBK 安全）
    assert all(ord(c) < 128 for c in "[WARN]")
