"""EmbeddingPort 端口（领域层拥有的抽象契约）。

依赖倒置：领域层依赖本协议做文本向量化，集成层 EmbeddingService 结构实现之。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingPort(Protocol):
    """文本向量化抽象：领域层对嵌入需求的唯一依赖面。"""

    async def embed(self, text: str, model: str | None = None) -> list[float]:
        """单文本向量化，返回向量数组。"""
        ...

    async def embed_batch(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """批量向量化（自动分批，结果保持输入顺序）。"""
        ...
