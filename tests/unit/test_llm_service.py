"""
LLMService 单元测试（Facade 编排层专属）

覆盖：
    generate() fallback 对称性   非流式路径同样构建并传入 fallback_fn（与 async_generate 对齐）
    下游调用参数透传            model_key / response_format / max_tokens

不依赖真实 API：mock ClientManager / RetryHandlerManager，构造假非流式响应。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.integration.llm.llm_service import LLMService


# =====================================================================
# 假响应 / 假组件
# =====================================================================


class _FakeUsage:
    def model_dump(self):
        return {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


class _FakeResponse:
    """模拟非流式 OpenAI 响应（parse_non_stream 消费：choices / usage）。"""

    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        message = SimpleNamespace(
            content=content, tool_calls=None, refusal=None
        )
        choice = SimpleNamespace(message=message, finish_reason=finish_reason)
        self.choices = [choice]
        self.usage = _FakeUsage()


class _FakeCompletions:
    """模拟 chat.completions.create：按序返回预置响应，记录调用 kwargs。"""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, completions) -> None:
        self.chat = SimpleNamespace(completions=completions)


class _FakeRetry:
    """模拟 RetryHandler.execute：记录是否收到 fallback_fn，并模拟主失败→走 fallback。"""

    def __init__(self, fallback_result) -> None:
        self.fallback_result = fallback_result
        self.execute_kwargs: dict | None = None

    async def execute(self, call_fn, fallback_fn=None):
        self.execute_kwargs = {"call_fn": call_fn, "fallback_fn": fallback_fn}
        # 模拟真实行为：主链路失败（重试耗尽）→ 调用 fallback_fn 兜底
        return await fallback_fn()


# =====================================================================
# 配额结算兜底测试辅助（LLM-002）
# =====================================================================


class _BrokenResponse:
    """模拟响应形状异常：访问 choices 抛 AttributeError（parse_non_stream 崩溃）。"""

    @property
    def choices(self):
        raise AttributeError("malformed response: missing choices")


class _TrackingReservation:
    """记录 settle/cancel 调用的 Reservation 桩（终态语义对齐真实实现）。"""

    def __init__(self) -> None:
        self.settled = False
        self.settle_calls = 0
        self.cancel_calls = 0

    async def settle(self, actual=None):
        self.settle_calls += 1
        self.settled = True

    async def cancel(self):
        self.cancel_calls += 1
        self.settled = True


class _CancelOnSettleReservation(_TrackingReservation):
    """settle 抛 CancelledError（模拟退款循环中途被取消，未到终态）。"""

    async def settle(self, actual=None):
        raise asyncio.CancelledError()


class _StubLimiter:
    """限流桩：reserve 返回预置 Reservation（记录结算路径）。"""

    def __init__(self, reservation) -> None:
        self._reservation = reservation

    async def reserve(self, estimated_tokens=0, retry_after=None):
        return self._reservation

    async def reserve_adaptive(self, prompt_tokens=0, max_tokens=0):
        return self._reservation


class _FakeRetryDirect:
    """retry.execute 直接调 call_fn（主链路成功，reserve 进 active）。"""

    async def execute(self, call_fn, fallback_fn=None):
        return await call_fn()


def _patch_generate_env(monkeypatch, client, retry, reservation):
    """patch 非流式 generate 依赖：ClientManager / RetryHandlerManager / ReservationLimiterManager。"""
    monkeypatch.setattr(
        "app.integration.llm.llm_service.ClientManager.get_model",
        staticmethod(lambda key: f"{key}-model"),
    )
    monkeypatch.setattr(
        "app.integration.llm.llm_service.ClientManager.get_client",
        staticmethod(lambda key: client),
    )
    monkeypatch.setattr(
        "app.integration.llm.llm_service.RetryHandlerManager.get",
        staticmethod(lambda key: retry),
    )
    monkeypatch.setattr(
        "app.integration.llm.llm_service.ReservationLimiterManager.get",
        staticmethod(lambda key: _StubLimiter(reservation)),
    )


def _patch_llm_env(monkeypatch, fallback_client, fake_retry):
    """patch ClientManager（get_model/get_client）与 RetryHandlerManager.get。"""
    monkeypatch.setattr(
        "app.integration.llm.llm_service.ClientManager.get_model",
        staticmethod(lambda key: f"{key}-model"),
    )
    monkeypatch.setattr(
        "app.integration.llm.llm_service.ClientManager.get_client",
        staticmethod(lambda key: fallback_client),
    )
    monkeypatch.setattr(
        "app.integration.llm.llm_service.RetryHandlerManager.get",
        staticmethod(lambda key: fake_retry),
    )


# =====================================================================
# generate() fallback 对称性（修复：非流式路径也应传入 fallback_fn）
# =====================================================================


@pytest.mark.asyncio
async def test_generate_passes_fallback_fn(monkeypatch):
    """generate() 应构建并传入 fallback_fn（与 async_generate 对称）。

    修复前：generate() 的 retry.execute 只传 call_fn，不传 fallback_fn——
    配置了 llm_fallback_model_id 时备用模型兜底只对流式主链路生效，非流式/
    结构化路径静默缺失该能力。
    修复后：generate() 同样构建并传入 fallback_fn，主链路失败时经 retry 兜底。
    """
    LLMService.register_config(
        fallback_model_id="fallback-model",
        adaptive_reserve=False,
        stream_max_retries=1,
    )
    try:
        llm = LLMService()
        completions = _FakeCompletions([_FakeResponse('{"name": "fallback"}')])
        fake_retry = _FakeRetry(_FakeResponse('{"name": "fallback"}'))
        _patch_llm_env(monkeypatch, _FakeClient(completions), fake_retry)

        result = await llm.generate(
            messages=[{"role": "user", "content": "张三"}],
        )
        assert result is not None
        assert result.content == '{"name": "fallback"}', "应返回 fallback 模型结果"
        assert fake_retry.execute_kwargs is not None
        assert fake_retry.execute_kwargs["fallback_fn"] is not None, (
            "generate() 应传入 fallback_fn（与 async_generate 对称）"
        )
        # fallback 调用实际走了备用模型（模型名被替换）
        assert completions.calls, "fallback_fn 应被调用"
        assert completions.calls[0]["model"] == "fallback-model", (
            "fallback 请求应使用备用模型"
        )
    finally:
        # 恢复类配置默认值，避免污染其他测试
        LLMService._fallback_model_id = ""
        LLMService._adaptive_reserve = False
        LLMService._stream_max_retries = 1


# =====================================================================
# _count_prompt_tokens content 归一化：None / 多模态 list 不抛 TypeError
# =====================================================================


class _FakeEncoder:
    """模拟 tiktoken 编码器：encode 只接受 str，非 str 抛 TypeError（对齐真实行为）。"""

    def encode(self, text):
        if not isinstance(text, str):
            raise TypeError(f"expected str, got {type(text).__name__}")
        return list(text)


def test_count_prompt_tokens_none_content(monkeypatch):
    """content 为 None（工具报错场景）→ 不抛 TypeError，按空串计数。

    修复前：msg.get("content", "") 在 content 键存在但值为 None 时返回
    None → encoder.encode(None) 抛 TypeError，限流预留阶段崩溃整次调用。
    """
    from app.integration.llm.llm_service import _count_prompt_tokens

    monkeypatch.setattr(
        "app.integration.llm.llm_service._get_encoder",
        staticmethod(lambda model: _FakeEncoder()),
    )
    monkeypatch.setattr(
        "app.integration.llm.llm_service.ClientManager.get_model",
        staticmethod(lambda key: "main-model"),
    )

    messages = [{"role": "user", "content": None}]  # 工具报错场景 content 为 None
    total = _count_prompt_tokens("main", messages, max_tokens=10)
    assert isinstance(total, int), "None content 不应抛异常，应返回 token 计数"


def test_count_prompt_tokens_multimodal_list_content(monkeypatch):
    """content 为多模态 list（[{"type":"text","text":...}]）→ 不抛 TypeError。

    修复前：encoder.encode(list) 抛 TypeError。修复后：归一化只取文本片段。
    """
    from app.integration.llm.llm_service import _count_prompt_tokens

    monkeypatch.setattr(
        "app.integration.llm.llm_service._get_encoder",
        staticmethod(lambda model: _FakeEncoder()),
    )
    monkeypatch.setattr(
        "app.integration.llm.llm_service.ClientManager.get_model",
        staticmethod(lambda key: "main-model"),
    )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "分析这张图"},
                {"type": "image_url", "image_url": {"url": "..."}},
            ],
        }
    ]
    total = _count_prompt_tokens("main", messages, max_tokens=10)
    assert isinstance(total, int), "多模态 list content 不应抛异常"
    assert total > 0, "多模态文本片段应计入 token"


# =====================================================================
# 配额结算兜底（LLM-002）：parse 异常 / settle 取消不泄漏
# =====================================================================


@pytest.mark.asyncio
async def test_generate_settles_on_parse_error(monkeypatch):
    """parse 抛异常 → finally 兜底 settle(None)，res 到终态不泄漏（LLM-002）。

    修复前：parse_non_stream 抛异常时 active["res"] 无人清理，配额永久占用。
    修复后：try/finally 内 settle 兜底；请求已发出 → settle 保留配额（非 cancel 全额退）。
    """
    reservation = _TrackingReservation()
    client = _FakeClient(_FakeCompletions([_BrokenResponse()]))
    _patch_generate_env(monkeypatch, client, _FakeRetryDirect(), reservation)

    llm = LLMService()
    with pytest.raises(AttributeError):
        await llm.generate(messages=[{"role": "user", "content": "hi"}])

    assert reservation.settled, "parse 异常应结算兜底（settle），不泄漏"
    assert reservation.settle_calls == 1
    assert reservation.cancel_calls == 0, "请求已发出，应 settle（保留配额）而非 cancel"


@pytest.mark.asyncio
async def test_generate_cancels_when_settle_cancelled(monkeypatch):
    """settle 被取消 → finally 兜底 cancel 未终态 res，不泄漏 + 传播取消（LLM-002）。

    修复前：settle 期间被硬取消，res 已 pop 且未终态，无人续退 → 配额泄漏。
    修复后：except 兜底 cancel 未终态 res + re-raise（不吞取消信号）。
    """
    reservation = _CancelOnSettleReservation()
    client = _FakeClient(_FakeCompletions([_FakeResponse("ok")]))
    _patch_generate_env(monkeypatch, client, _FakeRetryDirect(), reservation)

    llm = LLMService()
    with pytest.raises(asyncio.CancelledError):
        await llm.generate(messages=[{"role": "user", "content": "hi"}])

    assert reservation.cancel_calls == 1, "settle 取消后应 cancel 兜底（未终态 res 不泄漏）"
    assert reservation.settled, "cancel 后应到终态"
