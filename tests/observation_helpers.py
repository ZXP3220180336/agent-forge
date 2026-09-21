"""非关键观测边界的测试注入器：让真实 logging handler 抛错。

为什么用真实 handler，而不是 monkeypatch ``logging.Logger.warning``：

- stdlib logging 对 ``handler.emit`` 的异常不设保护（``Logger.handle`` →
  ``Handler.handle`` → ``emit`` 之间没有 try），自定义 handler 抛错会真的逃出
  ``logger.error()``；而 ``StreamHandler`` / ``FileHandler`` 自身的 I/O 错误与参数
  格式化错误会被 ``handleError`` 吞掉、不传播。故障面取决于 handler 实现，因此只有
  真实 handler 才能复现。
- monkeypatch ``Logger.warning`` 会把「调用点是否真的接上了隔离边界」这一层一起糊掉：
  测试仍然通过，但证明不了被测代码做了什么。

``exploding_handler`` 会在必要时临时把 logger 等级降到 WARNING，避免用例因等级屏蔽而
**空转通过**；handler 上的 ``calls`` 计数供调用方断言观测确实被触发过。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


class ExplodingHandler(logging.Handler):
    """``emit`` 直接抛错——stdlib logging 的核心不会捕获它。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.calls += 1
        raise OSError("log handler unavailable")


@contextmanager
def exploding_handler(logger_name: str) -> Iterator[ExplodingHandler]:
    """在指定 logger 上临时挂一个会抛错的 handler，退出时移除并恢复等级。"""
    handler = ExplodingHandler()
    logger = logging.getLogger(logger_name)
    previous_level = logger.level
    if not logger.isEnabledFor(logging.WARNING):
        logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        handler.close()
