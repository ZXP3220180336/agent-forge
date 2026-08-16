"""
全局日志框架 — 项目级统一日志入口。

职责：
    1. setup_logging()  按 settings 配置双 handler（控制台人类可读 + 文件结构化）
    2. get_logger()     返回 app.* 命名空间下的标准 Logger，供各模块记普通日志
    3. log_event() / log_event_async()  业务事件日志（如 LLM 调用记录）

使用方式：
    # 各模块普通日志
    logger = get_logger("container")
    logger.warning("Redis 不可用，服务降级")

    # 业务事件（结构化字段进 JSON 输出）
    await log_event_async("llm_call", success=True, duration=2.3, total_tokens=120)

事件机制：event_name 作为 LogRecord 的 message，fields 经 extra 注入成为
LogRecord 的自定义属性，由 JsonFormatter 统一序列化到结构化输出。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "ConsoleFormatter",
    "JsonFormatter",
    "fill_llm_event_fields",
    "get_logger",
    "log_event",
    "log_event_async",
    "setup_logging",
]

# 业务事件统一走 app.events logger（事件名即 message，可跨模块检索）
_EVENT_LOGGER = logging.getLogger("app.events")

# LogRecord 保留键：formatter 侧白名单排除，避免 extra 注入与保留属性冲突。
# （若调用方误传保留键名，logging 会在调用点直接抛 KeyError，作为开发期 bug 暴露。）
_LOG_RESERVED = frozenset(
    [
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
        "asctime",
    ]
)

_LEVEL_GLYPH = {
    "DEBUG": "DBG",
    "INFO": "OK",
    "WARNING": "WARN",
    "ERROR": "ERR",
    "CRITICAL": "CRIT",
}


def get_logger(name: str) -> logging.Logger:
    """返回 app.* 命名空间下的标准 Logger。

    Args:
        name: 模块名（如 "container"）→ 返回 logger "app.container"
    """
    return logging.getLogger(f"app.{name}")


def setup_logging(
    level: str = "INFO",
    log_file: str = "logs/app.log",
    log_format: str = "json",
) -> None:
    """按传入配置初始化根 logger 的双 handler（幂等，可重复调用）。

    - 控制台：人类可读文本（ConsoleFormatter）
    - 文件：JSON 结构化（默认）或文本（log_format == "text"）
    重复调用会清空现有 handler 重建，支持改配置后重配。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）
        log_file: 日志文件路径
        log_format: 文件输出格式（json/text）
    """
    log_level = getattr(logging, level, logging.INFO)
    root = logging.getLogger()

    # 幂等：清空已有 handler 后重建（close 释放文件句柄）
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(log_level)

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    # 文件 handler（目录不存在则创建）
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(log_level)
    formatter = JsonFormatter() if log_format == "json" else ConsoleFormatter()
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def log_event(event_name: str, level: int = logging.INFO, **fields: Any) -> None:
    """同步记录一条业务事件。

    Args:
        event_name: 事件名，作为 LogRecord 的 message（如 "llm_call"）
        level: 日志级别（默认 INFO）
        fields: 事件字段，经 extra 注入成为记录的自定义属性，进入结构化输出
    """
    _EVENT_LOGGER.log(level, event_name, extra=fields)


async def log_event_async(
    event_name: str, level: int = logging.INFO, **fields: Any
) -> None:
    """异步记录业务事件：to_thread 内执行同步写入，避免文件 IO 阻塞事件循环。

    需在运行中的事件循环内调用；无循环场景用同步 log_event。
    """
    await asyncio.to_thread(log_event, event_name, level=level, **fields)


async def fill_llm_event_fields(
    event_fields: dict[str, Any],
    *,
    success: bool,
    duration: float,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    finish_reason: str | None = None,
) -> None:
    """填充 LLM 调用事件（llm_call）字段并记录。

    通用 LLM 事件日志工具：填充 success/error/duration/tokens/finish_reason
    到 event_fields 并 await log_event_async 落盘。由 LLM 服务层（LLMService、
    StreamingRectifier）复用，统一各调用点的日志填充与记录。
    """
    event_fields["success"] = success
    event_fields["error"] = error
    event_fields["duration"] = duration
    if usage:
        event_fields["prompt_tokens"] = usage.get("prompt_tokens")
        event_fields["completion_tokens"] = usage.get("completion_tokens")
        event_fields["total_tokens"] = usage.get("total_tokens")
    event_fields["finish_reason"] = finish_reason
    await log_event_async("llm_call", **event_fields)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """返回记录中非标准、可注入结构化输出的自定义字段（排除 LogRecord 保留键）。"""
    return {k: v for k, v in record.__dict__.items() if k not in _LOG_RESERVED}


class JsonFormatter(logging.Formatter):
    """文件 handler：每行一条 JSON，含标准字段 + 事件自定义字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),  # 事件记录即 event_name，如 "llm_call"
        }
        payload.update(_extra_fields(record))
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """控制台 handler：人类可读，级别→[OK]/[WARN] 前缀 + 紧凑字段摘要。

    级别前缀全 ASCII，避免非 GBK 字符在 Windows 控制台触发编码错误。
    """

    def format(self, record: logging.LogRecord) -> str:
        glyph = _LEVEL_GLYPH.get(record.levelname, record.levelname)
        extra = _extra_fields(record)
        suffix = " " + " ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
        return (
            f"{self.formatTime(record, '%H:%M:%S')} [{glyph}] "
            f"{record.name}: {record.getMessage()}{suffix}"
        )
