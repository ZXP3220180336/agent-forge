"""非关键观测的异常隔离边界（共享内核横切能力，对齐 G0-6 观测隔离）。

日志、metrics、tracing 等非业务关键观测的失败不能替换主业务异常或终态，也不能无限
阻塞收尾。本模块把「失败即忽略」这一条边界收敛为可复用的单一实现，避免每个终态、
请求与收尾入口各写一份内联 try/except。

为什么只有同步形态、没有对应的异步有界原语：同步日志在调用方线程内执行，阻塞不可
中断（见 issues/integration/tools/2026-09-19-observation-cancel-ownership.md
「不能强制中断堵住事件循环的任意 Python 代码」），因此只能隔离、不能限时；需要限时的
异步观测已有各自主张（如 `app/platform/observability/logger.py` 的 LLM 事件日志按私有
常量做有界等待）。把两者合成一个带可选 timeout 的签名会让调用方误以为同步观测也受
时限保护。

共享内核约束：只依赖标准库，不 import ``app.platform``——``app.domain`` 与
``app.shared`` 均不可依赖 platform（对齐 ``app/domain/reasoning/react.py`` 与
``app/shared/error_handling.py`` 的既有依赖方向）。本模块自身不记日志：观测失败时静默
返回，避免「隔离失败再触发一次观测」的递归。
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["isolate_observation"]


def isolate_observation(operation: Callable[[], object]) -> None:
    """执行非关键观测，隔离其异常，不改变调用方的业务语义。

    只捕获 ``Exception``：``asyncio.CancelledError`` / ``KeyboardInterrupt`` /
    ``GeneratorExit`` 属于 ``BaseException``，继续传播，取消语义不变。

    用法约束：**整个日志表达式必须写进 ``operation``**（含参数求值、``LogRecord``
    构造、handler 与 filter 的执行），否则参数求值会逃出隔离范围::

        isolate_observation(lambda: logger.warning("裁剪 %d 条", dropped))

    该边界只用于日志、指标与审计等非关键观测；业务异常必须在调用本函数之前抛出，
    不得借它吞掉业务失败。
    """
    try:
        operation()
    except Exception:  # noqa: BLE001 — best-effort 观测边界（含自定义 handler / filter 抛错）
        return
