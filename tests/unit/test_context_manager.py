"""app/application/context/context_manager.py ContextManager 单元测试

使用手写 _FakeSessionManager（无 mock 库），tiktoken 为真实使用。
"""

import pytest

from app.application.context.context_manager import ContextManager


class _FakeSessionManager:
    """记录 get_messages 调用参数，返回固定会话与历史"""

    def __init__(self, session=None, messages=None):
        self.session = session
        self.messages = messages or []
        self.calls: list[tuple[str, int | None]] = []

    async def get_session(self, session_id):
        return self.session

    async def get_messages(self, session_id, limit=None, offset=0):
        self.calls.append((session_id, limit))
        return self.messages


def test_count_tokens_basic():
    cm = ContextManager(_FakeSessionManager(), "gpt-4")
    assert cm.count_tokens("hello world") == 2


def test_count_messages_tokens_overhead():
    """每条消息 +4 开销，末尾 +2"""
    cm = ContextManager(_FakeSessionManager(), "gpt-4")
    msg = [{"role": "user", "content": "hello"}]
    assert cm.count_messages_tokens(msg) == 4 + cm.count_tokens("hello") + 2


def test_count_messages_tokens_with_name():
    """带 name 字段额外 +1"""
    cm = ContextManager(_FakeSessionManager(), "gpt-4")
    msg = [{"role": "user", "content": "hello", "name": "bob"}]
    assert cm.count_messages_tokens(msg) == 4 + cm.count_tokens("hello") + 1 + 2


def test_encoding_fallback_on_unknown_model():
    """未知模型名触发 KeyError → 回退 cl100k_base"""
    cm = ContextManager(_FakeSessionManager(), model_name="definitely-not-a-model")
    assert cm.encoder.name == "cl100k_base"


@pytest.mark.asyncio
async def test_build_messages_raises_when_session_missing():
    cm = ContextManager(_FakeSessionManager(session=None), "gpt-4")
    with pytest.raises(ValueError, match="Session s1 not found"):
        await cm.build_messages("s1", "hi")


@pytest.mark.asyncio
async def test_build_messages_assembles_system_history_user():
    """组装顺序：system + history + user；limit = max_rounds * 2"""
    fake = _FakeSessionManager(
        session={"system_prompt": "sys"},
        messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
    )
    cm = ContextManager(fake, "gpt-4")
    messages, total = await cm.build_messages("s1", "hello", max_rounds=20)

    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "hello"},
    ]
    assert total == cm.count_messages_tokens(messages)
    assert fake.calls == [("s1", 40)]


@pytest.mark.asyncio
async def test_build_messages_passes_custom_max_rounds():
    fake = _FakeSessionManager(session={"system_prompt": "sys"})
    cm = ContextManager(fake, "gpt-4")
    await cm.build_messages("s1", "hello", max_rounds=3)
    assert fake.calls == [("s1", 6)]


@pytest.mark.asyncio
async def test_build_messages_truncates_when_over_budget():
    """超预算时截断：保留 system 与 user，总 token 不超可用预算"""
    fake = _FakeSessionManager(
        session={"system_prompt": "sys"},
        messages=[
            {"role": "user", "content": f"history {i} " + "x" * 100}
            for i in range(6)
        ],
    )
    cm = ContextManager(fake, "gpt-4", max_context_tokens=40, max_output_tokens=4)
    messages, total = await cm.build_messages("s1", "hello")

    assert total <= 36  # available = 40 - 4
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "user"
    assert len(messages) < 8  # 历史被截断


def test_truncate_messages_keeps_system_and_user():
    """预算足够全部保留；预算极小仅保留 system + user"""
    cm = ContextManager(_FakeSessionManager(), "gpt-4")
    system = {"role": "system", "content": "sys"}
    user = {"role": "user", "content": "hello"}
    history = [{"role": "user", "content": f"h{i} " + "y" * 50} for i in range(3)]
    messages = [system, *history, user]

    all_kept = cm._truncate_messages(messages, 10_000)
    assert all_kept == messages

    minimal = cm._truncate_messages(messages, 5)
    assert minimal == [system, user]


def test_truncate_messages_keeps_newest_history_first():
    """预算不足时保留最新历史，丢弃最早"""
    cm = ContextManager(_FakeSessionManager(), "gpt-4")
    system = {"role": "system", "content": "sys"}
    user = {"role": "user", "content": "hello"}
    oldest = {"role": "user", "content": "old " + "z" * 100}  # 很长
    newest = {"role": "assistant", "content": "new"}  # 很短
    messages = [system, oldest, newest, user]

    # 预算恰好容纳 system + newest + user，再塞 oldest 会超
    budget = cm.count_messages_tokens([system, newest, user])
    result = cm._truncate_messages(messages, budget)

    assert result[0] == system
    assert result[-1] == user
    assert result[1] == newest
    assert oldest not in result
    assert cm.count_messages_tokens(result) <= budget
