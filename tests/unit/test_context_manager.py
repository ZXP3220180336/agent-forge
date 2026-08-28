"""app/application/context/context_manager.py ContextManager 单元测试

使用手写 _FakeSessionManager（无 mock 库），token 计数为真实 tiktoken
（经 TiktokenTokenCounter 注入，与实现层一致）。
"""

import pytest

from app.application.context.context_manager import ContextManager
from app.integration.llm.token_counter import TiktokenTokenCounter


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
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
    assert cm.count_tokens("hello world") == 2


def test_count_messages_tokens_overhead():
    """每条消息 +4 开销，末尾 +2"""
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
    msg = [{"role": "user", "content": "hello"}]
    assert cm.count_messages_tokens(msg) == 4 + cm.count_tokens("hello") + 2


def test_count_messages_tokens_with_name():
    """带 name 字段额外 +1"""
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
    msg = [{"role": "user", "content": "hello", "name": "bob"}]
    assert cm.count_messages_tokens(msg) == 4 + cm.count_tokens("hello") + 1 + 2


@pytest.mark.asyncio
async def test_build_messages_raises_when_session_missing():
    cm = ContextManager(_FakeSessionManager(session=None), TiktokenTokenCounter("gpt-4"))
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
    cm = ContextManager(fake, TiktokenTokenCounter("gpt-4"))
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
    cm = ContextManager(fake, TiktokenTokenCounter("gpt-4"))
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
    cm = ContextManager(fake, TiktokenTokenCounter("gpt-4"), max_context_tokens=40, max_output_tokens=4)
    messages, total = await cm.build_messages("s1", "hello")

    assert total <= 36  # available = 40 - 4
    assert messages[0]["role"] == "system"
    assert messages[-1]["role"] == "user"
    assert len(messages) < 8  # 历史被截断


def test_truncate_messages_keeps_system_and_user():
    """预算足够全部保留；预算极小仅保留 system + user"""
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
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
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
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


def test_trim_messages_recent_rounds():
    """trim_messages 轮次预算：保留前缀 + 最近 N 轮，assistant/tool 配对不切断。"""
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a1", "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "assistant", "content": "a2", "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
        {"role": "assistant", "content": "a3"},
    ]
    cm.trim_messages(messages, max_rounds=2, max_tokens=None)

    assert [m["role"] for m in messages] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    # 保留的是最近 2 轮：a2/t2 + a3（配对不切断）
    assert messages[2]["content"] == "a2"
    assert messages[3]["tool_call_id"] == "t2"
    assert messages[4]["content"] == "a3"


def test_trim_messages_token_budget():
    """trim_messages token 预算：超限逐轮丢最旧，保留 system/user 前缀。"""
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "x" * 500},
        {"role": "tool", "tool_call_id": "t1", "content": "y" * 500},
        {"role": "assistant", "content": "z" * 500},
        {"role": "tool", "tool_call_id": "t2", "content": "w" * 500},
    ]
    cm.trim_messages(messages, max_rounds=None, max_tokens=100)

    # 前缀保留
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # 大消息一轮都放不下 → 至少丢到只剩前缀
    assert len(messages) <= 4
    assert cm._estimate_messages_tokens(messages) <= 100


def test_trim_messages_none_noop():
    """trim_messages 预算参数均 None → 不裁剪。"""
    cm = ContextManager(_FakeSessionManager(), TiktokenTokenCounter("gpt-4"))
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a1"},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
    ]
    cm.trim_messages(messages, max_rounds=None, max_tokens=None)
    assert len(messages) == 3
