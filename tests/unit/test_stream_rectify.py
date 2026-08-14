"""
LLMService.async_generate 流式整流重试单元测试

覆盖「首 token 前中断 → 整流重试」决策（参考 langchain-failover 语义）：
    流在产出第一个 token 前中断 → 整流重试（重新 create + 重新迭代）
    已产出任何 token 后中断   → 不整流，记日志 + 错误事件
    create 阶段异常           → 绝不整流（retry.execute 已决定重试/熔断/fallback）
    整流条件复用 classify_error：RETRYABLE / RATE_LIMITED 才整流

不依赖真实 API：mock ClientManager，用 FakeClient + FakeStream 模拟流式中断。
"""

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import APIResponseValidationError, BadRequestError, RateLimitError

from app.config import settings
from app.integration.llm import (
    ClientManager,
    RetryConfig,
    RetryHandlerManager,
)
from app.integration.llm.reservation_limiter import ReservationLimiter, ReservationLimiterManager
from app.integration.llm.llm_service import LLMService
from app.domain.ports.llm_gateway import StreamResult


# =====================================================================
# Mock 基础设施
# =====================================================================


def _content_chunk(text: str):
    """产出回复文本的 chunk。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=text, tool_calls=None
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _reasoning_chunk(text: str):
    """产出思考文本的 chunk。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=text, content=None, tool_calls=None
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _finish_chunk(reason: str):
    """携带 finish_reason 的 chunk（不算"首 token"）。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=None, tool_calls=None
                ),
                finish_reason=reason,
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt: int, completion: int):
    """携带 usage 的 chunk（无 choices，不算"首 token"）。"""
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            model_dump=lambda: {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
        ),
    )


def _tool_call_chunk():
    """携带工具调用增量的 chunk（算"首 token"）。"""
    tc = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="search", arguments='{"query":"x"}'),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    reasoning_content=None, content=None, tool_calls=[tc]
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


class FakeStream:
    """异步可迭代流：按序 yield chunks，可在指定位置抛异常模拟中断。

    fail_at=0 → 首个 chunk 前抛（连接建立后立即断）
    fail_at=1 → 消费 1 个 chunk 后抛
    fail_at=None → 正常结束
    """

    def __init__(self, chunks, fail_at=None, exc=None):
        self._chunks = list(chunks)
        self._fail_at = fail_at
        self._exc = exc or httpx.ReadError("connection reset")
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._fail_at is not None and self._i >= self._fail_at:
            self._i += 1
            raise self._exc
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk


class FakeCompletions:
    """模拟 client.chat.completions：按脚本返回 FakeStream 或抛异常。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if not self._script:
            return FakeStream([])
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.completions = FakeCompletions(script)
        self.chat = SimpleNamespace(completions=self.completions)  # client.chat.completions.create


# =====================================================================
# 测试辅助
# =====================================================================


def _setup(monkeypatch, script, stream_max_retries=1):
    """mock ClientManager + 极简退避，返回 (llm_service, fake_completions, run)。"""
    # RetryHandlerManager 是跨请求共享缓存的熔断器：每个测试前 reset，
    # 避免前一个测试的熔断窗口污染后续测试（测试隔离）。
    RetryHandlerManager.reset()
    fake_client = FakeClient(script)  # 共享实例，calls 计数一致
    monkeypatch.setattr(
        ClientManager, "get_client", staticmethod(lambda key: fake_client)
    )
    monkeypatch.setattr(
        ClientManager, "get_model", staticmethod(lambda key: "test-model")
    )
    # 退避/重试配置：极短延迟 + 关闭抖动 + create 不内部重试（调用次数可确定）。
    # configure 注入（子模块不读 settings），内部会 reset 保证测试隔离。
    RetryHandlerManager.register_config(
        config=RetryConfig(
            max_retries=0,
            base_delay=0.001,
            max_delay=0.01,
            use_jitter=False,
        ),
    )
    monkeypatch.setattr(settings, "llm_stream_max_retries", stream_max_retries)

    # 限流 stub：reserve 立即放行，记录调用次数与 estimated_tokens，并统计 settle 退差。
    # 避免真实 Token Bucket 的等待拖慢测试，且能断言整流/重试会重新 reserve。
    calls = {"reserve": 0, "last_estimated": 0, "settle": 0, "settle_total": 0, "cancel": 0}

    class _NoopReservation:
        def __init__(self):
            self.settled = False

        async def settle(self, actual=None):
            self.settled = True
            calls["settle"] += 1
            if actual is not None:
                calls["settle_total"] += calls["last_estimated"] - actual

        async def cancel(self):
            self.settled = True
            calls["cancel"] += 1

    class _StubRateLimiter(ReservationLimiter):
        async def reserve(self, estimated_tokens=0, retry_after=None):
            calls["reserve"] += 1
            calls["last_estimated"] = estimated_tokens
            return _NoopReservation()

    monkeypatch.setattr(
        ReservationLimiterManager,
        "get",
        staticmethod(lambda key: _StubRateLimiter()),
    )

    completions = fake_client.completions
    llm = LLMService()  # 不传参 → 不触发 register_config

    async def run(messages=None):
        sr = StreamResult()
        events = []
        async for ev in llm.async_generate(
            messages=messages or [{"role": "user", "content": "hi"}],
            result=sr,
        ):
            events.append(ev)
        return sr, events

    return llm, completions, run, calls


# =====================================================================
# 用例矩阵
# =====================================================================


@pytest.mark.asyncio
async def test_pre_first_token_interrupt_rectifies(monkeypatch):
    """首 token 前中断 → 整流重试成功（calls==2，无 error，result 正确）。"""
    script = [
        # 尝试 1：yield usage 后流中断（usage 不算"首 token"）
        FakeStream([_usage_chunk(10, 0)], fail_at=1, exc=httpx.ReadError("reset")),
        # 尝试 2：正常完整流
        FakeStream([_content_chunk("你"), _content_chunk("好"), _finish_chunk("stop"), _usage_chunk(10, 5)]),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 2, "应整流重试一次"
    assert all("error" not in e for e in events), f"不应有 error 事件: {events}"
    assert sr.content == "你好", f"content 应完整: {sr.content!r}"
    assert sr.finish_reason == "stop"
    assert sr.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
    }, "usage 应为尝试 2 的值"


@pytest.mark.asyncio
async def test_post_token_interrupt_no_rectify(monkeypatch):
    """已产出 token 后中断 → 不整流（calls==1，保留已产出内容 + error）。"""
    script = [
        FakeStream([_content_chunk("你好"), _finish_chunk("stop")], fail_at=1),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 1, "已产出 token 后中断不应整流重试"
    assert sr.content == "你好", "已产出内容应保留"
    assert any("error" in e for e in events), "应产出 error 事件"


@pytest.mark.asyncio
async def test_consecutive_interrupt_exhausts_retries(monkeypatch):
    """连续中断超上限 → 失败（calls==2，error 事件，content 为空）。"""
    script = [
        FakeStream([], fail_at=0),
        FakeStream([], fail_at=0),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 2, "应尝试 2 次（1 次整流 + 1 次原始）"
    assert any("error" in e for e in events), "应产出 error 事件"
    assert sr.content == "", "失败时 content 应为空"


@pytest.mark.asyncio
async def test_cancel_event_no_rectify(monkeypatch):
    """用户取消 → 不整流（calls==1，error='用户取消了请求'）。"""
    cancel = asyncio.Event()
    cancel.set()
    script = [
        FakeStream([_content_chunk("你好")]),  # 正常流，但迭代前 cancel 已置位
    ]
    fake_client = FakeClient(script)
    monkeypatch.setattr(
        ClientManager, "get_client", staticmethod(lambda key: fake_client)
    )
    monkeypatch.setattr(
        ClientManager, "get_model", staticmethod(lambda key: "test-model")
    )
    RetryHandlerManager.register_config(
        config=RetryConfig(
            max_retries=0,
            base_delay=0.001,
            max_delay=0.01,
            use_jitter=False,
        ),
    )
    monkeypatch.setattr(settings, "llm_stream_max_retries", 3)  # 即使可重试也不整流

    completions = fake_client.completions
    llm = LLMService()

    sr = StreamResult()
    events = []
    async for ev in llm.async_generate(
        messages=[{"role": "user", "content": "hi"}],
        result=sr,
        cancel_event=cancel,
    ):
        events.append(ev)

    assert completions.calls == 1, "取消后不应整流重试"
    assert any("用户取消了请求" in e for e in events), f"应有取消错误: {events}"


@pytest.mark.asyncio
async def test_create_failure_no_rectify(monkeypatch):
    """create 阶段失败（NON_RETRYABLE）→ 绝不整流（calls==1）。"""
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    script = [BadRequestError("bad request", response=resp, body=None)]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=3)

    sr, events = await run()

    assert completions.calls == 1, "create 失败不得整流重试"
    assert any("LLM 调用失败" in e for e in events), "应产出 LLM 调用失败事件"
    assert sr.content == ""


@pytest.mark.asyncio
async def test_rectify_then_create_failure(monkeypatch):
    """整流尝试的下一轮 create 失败 → 走 create 失败路径，不再整流。"""
    resp = httpx.Response(500, request=httpx.Request("POST", "http://x"))
    script = [
        FakeStream([], fail_at=0),  # 尝试 1：死流 → 整流
        TimeoutError("create timeout"),  # 尝试 2：create 抛超时（max_retries=0 不重试）
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 2
    assert any("LLM 调用失败" in e for e in events), "应走 create 失败路径"
    assert sr.content == ""


@pytest.mark.asyncio
async def test_tool_call_delta_counts_as_emitted(monkeypatch):
    """tool_call_deltas 算已产出 → 中断后不整流（calls==1）。"""
    script = [
        FakeStream([_tool_call_chunk()], fail_at=1),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 1, "已产出 tool_call 增量不应整流"
    assert any("error" in e for e in events), "应产出 error 事件"
    assert sr.tool_calls == [], "失败时不应 merge tool_calls"


@pytest.mark.asyncio
async def test_usage_only_interrupt_rectifies(monkeypatch):
    """仅 usage/finish_reason 后中断 → 整流（残留元数据被清空）。"""
    script = [
        # 尝试 1：yield finish_reason 后断（不算"首 token"）
        FakeStream([_finish_chunk("stop")], fail_at=1),
        # 尝试 2：正常流
        FakeStream([_content_chunk("ok"), _finish_chunk("stop"), _usage_chunk(1, 2)]),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 2, "仅 finish_reason 后中断应整流"
    assert sr.content == "ok"
    assert sr.usage == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }, "死流 usage 残留应被清空，取尝试 2 的值"
    assert sr.finish_reason == "stop"


@pytest.mark.asyncio
async def test_success_path_regression(monkeypatch):
    """成功路径回归：正常流不整流（calls==1，无 error）。"""
    script = [
        FakeStream([_content_chunk("hi"), _finish_chunk("stop"), _usage_chunk(1, 2)]),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 1
    assert all("error" not in e for e in events), f"不应有 error: {events}"
    assert sr.content == "hi"
    assert sr.finish_reason == "stop"


@pytest.mark.asyncio
async def test_non_retryable_iter_exception_no_rectify(monkeypatch):
    """迭代异常为 NON_RETRYABLE（响应校验错误）→ 不整流。"""
    resp = httpx.Response(200, request=httpx.Request("POST", "http://x"))
    exc = APIResponseValidationError(
        response=resp, body=None, message="schema mismatch"
    )
    script = [
        FakeStream([], fail_at=0, exc=exc),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=3)

    sr, events = await run()

    assert completions.calls == 1, "NON_RETRYABLE 迭代异常不得整流"
    assert any("error" in e for e in events), "应产出 error 事件"


@pytest.mark.asyncio
async def test_logging_records_rectified_attempts(monkeypatch, caplog):
    """整流成功后日志：1 条失败（流式读取中断）+ 1 条成功。"""
    script = [
        FakeStream([_usage_chunk(10, 0)], fail_at=1, exc=httpx.ReadError("reset")),
        FakeStream([_content_chunk("ok"), _finish_chunk("stop"), _usage_chunk(10, 5)]),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    with caplog.at_level(logging.INFO, logger="app.events"):
        await run()

    records = caplog.records
    assert len(records) == 2, f"整流成功应有 2 条日志，实际 {len(records)}: {records}"
    assert all(r.getMessage() == "llm_call" for r in records), "事件名应为 llm_call"
    assert records[0].success is False, "第 1 条应为失败日志"
    assert "流式读取中断" in records[0].error, f"失败原因: {records[0].error}"
    assert records[1].success is True, "第 2 条应为成功日志"
    assert records[1].error is None, "成功日志不应有错误"
    assert all(r.duration >= 0 for r in records), "duration 应为非负"


@pytest.mark.asyncio
async def test_rate_limiter_acquire_before_each_attempt(monkeypatch):
    """限流接入：整流重试时每轮 call_fn 都 acquire（calls==2 次，estimated>0）。"""
    script = [
        FakeStream([_usage_chunk(10, 0)], fail_at=1, exc=httpx.ReadError("reset")),
        FakeStream([_content_chunk("ok"), _finish_chunk("stop"), _usage_chunk(10, 5)]),
    ]
    _, completions, run, calls = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 2, "整流重试"
    assert calls["reserve"] == 2, f"整流 2 轮各 reserve 一次，实际 {calls['reserve']} 次"
    assert calls["last_estimated"] > 0, "estimated_tokens 应为 messages 的 tiktoken 计数"
    # 每次尝试都 settle（成功那次按 usage 退差，死流那次 settle(None) 保守）
    assert calls["settle"] == 2, f"每轮 create 成功后应 settle，实际 {calls['settle']} 次"
    assert calls["cancel"] == 0, "create 成功后不应 cancel（请求已发出）"
    assert sr.content == "ok"
    assert all("error" not in e for e in events), f"不应有 error: {events}"


@pytest.mark.asyncio
async def test_rate_limiter_acquire_on_retry_inside_execute(monkeypatch):
    """限流接入（核心）：retry.execute 内部重试也 acquire。

    create 阶段第 1 次抛 RETRYABLE 异常（httpx.ReadError）→ retry 内部重试第 2 次。
    acquire 位于 call_fn 内，每次真实调用前都 acquire → 应调用 2 次。
    """
    script = [
        httpx.ReadError("connection reset"),  # create 第 1 次：可重试 → retry 重试
        FakeStream([_content_chunk("ok"), _finish_chunk("stop"), _usage_chunk(10, 5)]),
    ]
    _, completions, run, calls = _setup(monkeypatch, script, stream_max_retries=1)
    # retry.execute 内部重试需要 max_retries >= 1（configure 覆盖 _setup 的 0）
    RetryHandlerManager.register_config(
        config=RetryConfig(
            max_retries=1,
            base_delay=0.001,
            max_delay=0.01,
            use_jitter=False,
        ),
    )

    sr, events = await run()

    assert completions.calls == 2, "create 应重试 1 次"
    assert calls["reserve"] == 2, (
        f"重试也应 reserve（每次 call_fn 调用前一次），实际 {calls['reserve']} 次"
    )
    # 第 1 次 create 失败在 call_fn 内 cancel 全额退；第 2 次成功 settle 退差
    assert calls["cancel"] == 1, f"create 失败应 cancel 全额退，实际 {calls['cancel']} 次"
    assert calls["settle"] == 1, f"成功那次应 settle，实际 {calls['settle']} 次"
    assert sr.content == "ok"
    assert all("error" not in e for e in events), f"不应有 error: {events}"


@pytest.mark.asyncio
async def test_rate_limiter_settle_refunds_overestimate(monkeypatch):
    """结算退差：成功路径按 usage.total_tokens 退差（预估 > 实际）。"""
    script = [
        FakeStream(
            [_content_chunk("ok"), _finish_chunk("stop"), _usage_chunk(10, 5)]
        ),
    ]
    _, completions, run, calls = _setup(monkeypatch, script, stream_max_retries=0)

    await run()

    # estimated = prompt(10 的计数) + max_tokens，必 > actual total(15) → settle 退正数
    assert calls["settle"] == 1, "成功路径应 settle"
    assert calls["cancel"] == 0, "成功路径不应 cancel"
    assert calls["settle_total"] > 0, f"应退还预估多余部分: {calls['settle_total']}"
    assert calls["last_estimated"] > 15, "estimated 应含 max_tokens 输出余量"


@pytest.mark.asyncio
async def test_rate_limiter_cancel_on_create_failure(monkeypatch):
    """create 失败全额退：call_fn 内 cancel，不 settle。"""
    script = [
        httpx.ReadError("connection reset"),  # create 失败 → cancel 全额退
    ]
    _, completions, run, calls = _setup(monkeypatch, script, stream_max_retries=0)

    sr, events = await run()

    assert completions.calls == 1
    assert calls["cancel"] == 1, f"create 失败应 cancel，实际 {calls['cancel']} 次"
    assert calls["settle"] == 0, "create 失败不应 settle"
    assert any("error" in e for e in events), "应产出 error 事件"


# =====================================================================
# 补熔断观察盲区：流式迭代「放弃时」喂 record_failure
# =====================================================================
# 背景：流式迭代异常不受 retry.execute 保护（响应创建后重试循环已退出），
# 若 create 正常但流频繁中途断开，熔断器此前不感知。修复后，最终放弃
# （不整流）且异常为 RETRYABLE 时喂 record_failure()。
# 各测试通过 RetryHandlerManager 的共享熔断器断言 failure_count。


def _cb_failure_count() -> int:
    """获取 main 模型共享熔断器的窗口失败数（_setup 已 reset，各测试独立）。"""
    return RetryHandlerManager.get("main").circuit_breaker.failure_count


@pytest.mark.asyncio
async def test_iter_interrupt_after_token_feeds_breaker(monkeypatch):
    """已产出 token 后中断（放弃，RETRYABLE）→ 喂 record_failure。"""
    script = [
        FakeStream(
            [_content_chunk("部分"), _finish_chunk("stop")],
            fail_at=1,  # 消费 1 个 content chunk 后中断
            exc=httpx.ReadError("connection reset"),
        ),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    await run()

    assert completions.calls == 1, "已产出 token 后中断不整流，仅 1 次调用"
    assert _cb_failure_count() == 1, "放弃的 RETRYABLE 迭代中断应计入熔断窗口"


@pytest.mark.asyncio
async def test_rectified_then_success_not_feeds_breaker(monkeypatch):
    """首 token 前中断→整流→成功：整流成功不喂失败（下游自愈）。"""
    script = [
        FakeStream([], fail_at=0, exc=httpx.ReadError("connection reset")),  # 死流
        FakeStream([_content_chunk("ok"), _finish_chunk("stop")]),  # 整流成功
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 2, "整流后成功应 2 次调用"
    assert sr.content == "ok"
    assert not any("error" in e for e in events), "整流成功不应产出 error"
    assert _cb_failure_count() == 0, "整流后成功不应计入熔断窗口"


@pytest.mark.asyncio
async def test_rectify_exhausted_then_abandon_feeds_once(monkeypatch):
    """连续死流耗尽整流上限后放弃：仅最终放弃记一次失败，不重复记每次中断。"""
    script = [
        FakeStream([], fail_at=0, exc=httpx.ReadError("connection reset")),  # 第 1 轮死流
        FakeStream([], fail_at=0, exc=httpx.ReadError("connection reset")),  # 第 2 轮死流
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    sr, events = await run()

    assert completions.calls == 2, "连续死流耗尽整流上限（2 次调用）"
    assert any("error" in e for e in events), "放弃应产出 error"
    # 只有最终放弃喂一次失败（整流过程的中断不算——整流是重新尝试，未放弃）
    assert _cb_failure_count() == 1, "应仅最终放弃记一次失败"


@pytest.mark.asyncio
async def test_non_retryable_iter_exception_not_feeds_breaker(monkeypatch):
    """NON_RETRYABLE 迭代异常（客户端问题）→ 不喂 record_failure。"""
    resp = httpx.Response(200, request=httpx.Request("POST", "http://x"))
    exc = APIResponseValidationError(
        response=resp, body=None, message="schema mismatch"
    )
    script = [
        FakeStream([], fail_at=0, exc=exc),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    await run()

    assert completions.calls == 1, "NON_RETRYABLE 不整流，仅 1 次调用"
    assert _cb_failure_count() == 0, "NON_RETRYABLE（客户端问题）不应计入熔断窗口"


@pytest.mark.asyncio
async def test_rate_limited_iter_exception_not_feeds_breaker(monkeypatch):
    """RATE_LIMITED 迭代异常（429 限流）→ 不喂 record_failure。"""
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    exc = RateLimitError("rate limited", response=resp, body=None)
    script = [
        FakeStream([], fail_at=0, exc=exc),
    ]
    # stream_max_retries=0 禁用整流 → 429 迭代异常直接走放弃分支
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=0)

    await run()

    assert completions.calls == 1, "禁整流下 429 仅 1 次调用"
    assert _cb_failure_count() == 0, "429（限流）不应计入熔断窗口"


@pytest.mark.asyncio
async def test_cancel_event_not_feeds_breaker(monkeypatch):
    """cancel_event 置位（用户取消，非下游故障）→ 不喂 record_failure。"""
    script = [
        FakeStream([_content_chunk("ok")], fail_at=None),
    ]
    _, completions, run, _ = _setup(monkeypatch, script, stream_max_retries=1)

    cancel_event = asyncio.Event()
    cancel_event.set()  # 置位 → 迭代内取消检查触发

    sr = StreamResult()
    events = []
    llm = LLMService()
    async for ev in llm.async_generate(
        messages=[{"role": "user", "content": "hi"}],
        result=sr,
        cancel_event=cancel_event,
    ):
        events.append(ev)

    assert any("error" in e for e in events), "取消应产出 error 事件"
    assert _cb_failure_count() == 0, "用户取消（非下游故障）不应计入熔断窗口"
