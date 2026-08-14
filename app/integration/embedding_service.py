"""
EmbeddingService — 文本向量化服务

封装 OpenAI 兼容的 Embedding API，支持批量嵌入和缓存。

用法：
    emb = EmbeddingService(client=AsyncOpenAI(...))
    vector = await emb.embed("Hello world")
    vectors = await emb.embed_batch(["Hello", "World"])
"""

from __future__ import annotations

import hashlib

from openai import AsyncOpenAI


class EmbeddingService:
    """
    向量化服务。

    支持：
    - 单文本嵌入
    - 批量嵌入（自动分批，每批 max_batch_size 条）
    - 可选内存缓存（去重）

    注意：缓存仅在同一实例生命周期内有效。
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        max_batch_size: int = 20,
        enable_cache: bool = True,
    ) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._max_batch_size = max_batch_size
        self._cache: dict[str, list[float]] = {} if enable_cache else None

    async def embed(
        self,
        text: str,
        model: str | None = None,
    ) -> list[float]:
        """
        单文本嵌入。

        Args:
            text: 输入文本
            model: 模型名，默认使用构造时指定的模型

        Returns:
            向量数组（维度由 self._dimensions 指定）
        """
        result = await self.embed_batch([text], model=model)
        return result[0]

    async def embed_batch(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """
        批量嵌入。

        自动分批，利用缓存去重。
        对每一批调用 Embedding API，返回结果保持输入顺序。

        Args:
            texts: 输入文本列表
            model: 模型名

        Returns:
            向量数组列表，顺序与输入相同
        """
        if not texts:
            return []

        model_name = model or self._model
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        # 查缓存
        for i, t in enumerate(texts):
            key = self._make_cache_key(t, model_name)
            if self._cache is not None and key in self._cache:
                results[i] = self._cache[key]
            else:
                uncached_indices.append(i)
                uncached_texts.append(t)

        if not uncached_texts:
            return results  # type: ignore[return-value]

        # 分批调用 API
        for batch_start in range(0, len(uncached_texts), self._max_batch_size):
            batch = uncached_texts[batch_start : batch_start + self._max_batch_size]
            batch_indices = uncached_indices[
                batch_start : batch_start + self._max_batch_size
            ]

            response = await self._client.embeddings.create(
                model=model_name,
                input=batch,
                dimensions=self._dimensions,
            )

            # 结果按输入顺序返回
            for j, data in enumerate(response.data):
                idx = batch_indices[j]
                vector = data.embedding
                results[idx] = vector

                # 写入缓存
                if self._cache is not None:
                    key = self._make_cache_key(batch[j], model_name)
                    self._cache[key] = vector

        return results  # type: ignore[return-value]

    def clear_cache(self) -> None:
        """清空缓存。"""
        if self._cache is not None:
            self._cache.clear()

    @property
    def cache_size(self) -> int:
        """缓存条目数。"""
        return len(self._cache) if self._cache is not None else 0

    @staticmethod
    def _make_cache_key(text: str, model: str) -> str:
        """生成缓存键（文本哈希 + 模型名）。"""
        text_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        return f"{model}:{text_hash}"
