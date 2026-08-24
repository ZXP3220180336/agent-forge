"""app/integration/embedding/embedding_service.py EmbeddingService 单元测试

手写 _FakeClient 模拟 AsyncOpenAI.embeddings.create（项目风格：不用 AsyncMock）。
向量维度固定 3，第 i 条输入返回 [i, i+1, i+2]，便于保序断言。
"""

from types import SimpleNamespace

from app.integration.embedding.embedding_service import EmbeddingService


class _FakeEmbeddings:
    """记录调用参数，按输入条数返回向量（第 i 条 = [i, i+1, i+2]）"""

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, model, input, dimensions):
        self.calls.append({"model": model, "input": input, "dimensions": dimensions})
        data = [
            SimpleNamespace(embedding=[float(i + j) for j in range(3)])
            for i, _ in enumerate(input)
        ]
        return SimpleNamespace(data=data)


class _FakeClient:
    def __init__(self):
        self.embeddings = _FakeEmbeddings()


def _make_service(**kwargs):
    defaults = dict(
        client=_FakeClient(),
        model="text-embedding-3-small",
        dimensions=3,
        max_batch_size=20,
        enable_cache=True,
    )
    defaults.update(kwargs)
    return EmbeddingService(**defaults)


async def test_embed_single():
    client = _FakeClient()
    service = EmbeddingService(client=client, dimensions=3)
    vec = await service.embed("hi")
    assert len(vec) == 3
    assert client.embeddings.calls[0]["model"] == "text-embedding-3-small"
    assert client.embeddings.calls[0]["input"] == ["hi"]
    assert client.embeddings.calls[0]["dimensions"] == 3


async def test_embed_delegates_to_batch():
    """embed 内部走 embed_batch([text])[0]"""
    service = _make_service()
    vec = await service.embed("hi")
    assert vec == [0.0, 1.0, 2.0]


async def test_embed_batch_preserves_order():
    """批量结果保持输入顺序（按输入位置区分向量值）"""
    service = _make_service()
    vectors = await service.embed_batch(["a", "b", "c"])
    assert len(vectors) == 3
    assert vectors[0] == [0.0, 1.0, 2.0]
    assert vectors[1] == [1.0, 2.0, 3.0]
    assert vectors[2] == [2.0, 3.0, 4.0]


async def test_embed_batch_chunks():
    """超过 max_batch_size 自动切批"""
    client = _FakeClient()
    service = EmbeddingService(client=client, max_batch_size=2)
    vectors = await service.embed_batch(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert len(client.embeddings.calls) == 3  # 5 条 / 每批 2 = 3 批
    assert client.embeddings.calls[0]["input"] == ["a", "b"]
    assert client.embeddings.calls[1]["input"] == ["c", "d"]
    assert client.embeddings.calls[2]["input"] == ["e"]


async def test_cache_hit_no_second_call():
    """同文本二次调用命中缓存，不调 API"""
    client = _FakeClient()
    service = EmbeddingService(client=client)
    await service.embed("hi")
    await service.embed("hi")
    assert len(client.embeddings.calls) == 1


async def test_cache_penetration_only_uncached():
    """部分命中时只请求未命中的文本，结果保序"""
    client = _FakeClient()
    service = EmbeddingService(client=client)
    await service.embed("a")
    await service.embed_batch(["a", "b", "c"])
    assert len(client.embeddings.calls) == 2
    assert client.embeddings.calls[1]["input"] == ["b", "c"]
    assert service.cache_size == 3


async def test_disable_cache():
    """enable_cache=False：无缓存，重复调用都走 API，cache_size 恒 0"""
    client = _FakeClient()
    service = EmbeddingService(client=client, enable_cache=False)
    assert service.cache_size == 0
    await service.embed("hi")
    await service.embed("hi")
    assert len(client.embeddings.calls) == 2
    service.clear_cache()  # no-op，不崩
    assert service.cache_size == 0


async def test_empty_input_returns_empty():
    service = _make_service()
    assert await service.embed_batch([]) == []


async def test_clear_cache():
    service = _make_service()
    await service.embed("hi")
    assert service.cache_size == 1
    service.clear_cache()
    assert service.cache_size == 0
