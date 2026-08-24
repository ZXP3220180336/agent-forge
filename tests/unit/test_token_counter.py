"""app/integration/llm/token_counter.py TiktokenTokenCounter 单元测试

tiktoken 为真实使用（与 test_context_manager 一致，不 mock 编码器）。
"""

from app.integration.llm.token_counter import TiktokenTokenCounter, get_encoder


def test_count_tokens_basic():
    counter = TiktokenTokenCounter("gpt-4")
    assert counter.count_tokens("hello world") == 2


def test_count_messages_tokens_overhead():
    """每条消息 +4 开销，末尾 +2"""
    counter = TiktokenTokenCounter("gpt-4")
    msg = [{"role": "user", "content": "hello"}]
    assert counter.count_messages_tokens(msg) == 4 + counter.count_tokens("hello") + 2


def test_count_messages_tokens_with_name():
    """带 name 字段额外 +1"""
    counter = TiktokenTokenCounter("gpt-4")
    msg = [{"role": "user", "content": "hello", "name": "bob"}]
    assert counter.count_messages_tokens(msg) == 4 + counter.count_tokens("hello") + 1 + 2


def test_encoding_fallback_on_unknown_model():
    """未知模型名触发 KeyError → 回退 cl100k_base"""
    counter = TiktokenTokenCounter("definitely-not-a-model")
    assert get_encoder("definitely-not-a-model").name == "cl100k_base"


def test_count_messages_content_none_defensive():
    """content 键存在但为 None 不崩溃（工具调用场景）"""
    counter = TiktokenTokenCounter("gpt-4")
    msg = [{"role": "user", "content": None}]
    assert counter.count_messages_tokens(msg) == 4 + 0 + 2


def test_count_messages_content_multimodal_list():
    """多模态 list content 只计文本片段"""
    counter = TiktokenTokenCounter("gpt-4")
    msg = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "..."}},
            ],
        }
    ]
    assert counter.count_messages_tokens(msg) == 4 + counter.count_tokens("hello") + 2


def test_get_encoder_cached_idempotent():
    """同一模型名复用缓存 encoder（同一对象）"""
    assert get_encoder("gpt-4") is get_encoder("gpt-4")
