"""成本上限护栏端口（领域层拥有的抽象契约）。

依赖倒置：领域层 Agent 依赖本协议，应用层 CostLimiter 结构实现之
（横切能力——Agent 运行中的成本治理统一归成本限流器，所有模式共享，
对齐 ContextBudgetPort 端口先例）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CostLimiterPort(Protocol):
    """Agent 运行中成本上限护栏。

    对齐工业界 cost ceiling（Claude Agent SDK / OpenAI）：ReAct 循环每轮累计
    token 用量，折算成本后与上限比较，超限即触发停机（经错误分发
    COST_EXCEEDED → 默认 STOP 降级）。判定与计算均为同步纯函数（无 IO，
    无内部可变状态）——实现可被容器安全共享为单例，并发请求不串扰。
    """

    def check(self, usage: dict) -> tuple[bool, float]:
        """判定累计 usage 折算成本是否超限，并返回当前累计成本。

        Args:
            usage: **跨轮累计**的 token 用量 {"prompt_tokens": int,
                "completion_tokens": int, "total_tokens": int}（非单轮）。

        Returns:
            (exceeded, cost_usd)：exceeded=True 表示成本已超限（应停机）；
            cost_usd 为本次折算的累计成本（美元），供停机错误文案与观测。
        """
        ...
