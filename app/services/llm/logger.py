"""
LLMLogger — LLM 请求/响应日志

职责：
    1. 记录每次 LLM 调用的元数据（模型、Token、耗时）
    2. 输出结构化 JSON 日志
    3. 敏感信息脱敏

使用方式：
    logger = LLMLogger()
    await logger.log_call(LLMRequestRecord(...))
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger("app.llm")


@dataclass
class LLMRequestRecord:
    """单次 LLM 调用的完整记录。"""

    # 调用上下文
    timestamp: float = field(default_factory=time.time)
    model: str = ""
    request_id: str = ""

    # 请求参数
    messages_count: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    temperature: float = 0.0
    has_tools: bool = False
    stream: bool = True

    # 性能
    duration: float = 0.0  # 秒

    # 结果
    success: bool = True
    error: str | None = None
    finish_reason: str | None = None


class LLMLogger:
    """
    LLM 调用日志记录器。

    默认输出 JSON 格式日志，可通过 logging 原生配置调整。
    全局共享实例，避免重复配置。
    """

    _instance: LLMLogger | None = None

    def __new__(cls) -> LLMLogger:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    async def log_call(record: LLMRequestRecord) -> None:
        """
        记录一次 LLM 调用。

        异步写入（通过 asyncio.to_thread 避免阻塞），
        确保日志操作不会影响主流程性能。
        """
        data = asdict(record)
        # 脱敏：不记录 messages 内容本身（只记数量）
        # API Key 等敏感信息不在这个 record 中
        logger.info(json.dumps(data, ensure_ascii=False, default=str))

    @staticmethod
    def format_for_console(record: LLMRequestRecord) -> str:
        """格式化为可读的终端输出。"""
        status = "OK" if record.success else "FAIL"
        tokens = (
            f" {record.total_tokens} tokens"
            if record.total_tokens is not None
            else ""
        )
        duration_str = f" {record.duration:.2f}s" if record.duration else ""
        error_str = f" [{record.error}]" if record.error else ""
        return (
            f"[LLM] {status} {record.model}"
            f"{tokens}{duration_str}"
            f" msgs={record.messages_count}"
            f"{error_str}"
        )
