"""
LLMService 单元测试（Facade 编排层专属）

覆盖：
    generate() fallback 对称性   非流式路径同样构建并传入 fallback_fn（与 async_generate 对齐）
    下游调用参数透传            model_key / response_format / max_tokens

不依赖真实 API：mock ClientManager / RetryHandlerManager，构造假非流式响应。
"""

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
