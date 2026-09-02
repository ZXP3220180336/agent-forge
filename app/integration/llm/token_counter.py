"""tiktoken 计数实现 — TokenCounter 端口的集成层适配器。

模块级函数 `get_encoder` / `content_to_text` 供 llm_service 复用
（消除 encoder 解析与 content 归一化的重复实现，单一事实源）；
类 `TiktokenTokenCounter` 实现端口 `TokenCounter`，供 ContextManager 注入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.shared.types import Messages

if TYPE_CHECKING:
    import tiktoken  # 注解-only：tiktoken 运行期仍由 get_encoder 内 lazy import


# =====================================================================
# 编码器解析（进程内缓存）
# =====================================================================

_encoder_cache: dict[str, tiktoken.Encoding] = {}


def get_encoder(model: str) -> tiktoken.Encoding:
    """按模型名解析 tiktoken 编码器（进程内缓存，未知模型回退 cl100k_base）。

    与 llm_service 原有 `_get_encoder` 语义一致；此处为单一事实源。
    """
    if model in _encoder_cache:
        return _encoder_cache[model]
    try:
        import tiktoken

        encoder = tiktoken.encoding_for_model(model)
    except KeyError:
        # 未知模型无专属编码器 → 回退通用 cl100k_base（token 估算足够）。
        # 只捕获 KeyError：tiktoken 缺失（ImportError）是硬依赖损坏，应自然
        # 传播 fail fast，不被此兜底掩盖。
        encoder = tiktoken.get_encoding("cl100k_base")
    _encoder_cache[model] = encoder
    return encoder


def content_to_text(content: Any) -> str:
    """将消息 content 归一化为可编码文本（供 token 估算）。

    - None（工具报错等缺 content 场景）→ 空串
    - str → 原样
    - 多模态 list（OpenAI 格式 `[{"type": "text", "text": ...}, ...]`）→
      只取文本片段拼接；图片等非文本条目不参与 token 估算

    `encoder.encode(None)` 抛 TypeError——content 键存在但为 None 时
    直接 `msg.get("content", "")` 兜不住，必须经此归一化。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts)
    # 非 str/list 的异常形状：不崩，保守回退空串（宁可低估不崩）
    return ""


class TiktokenTokenCounter:
    """tiktoken 实现的 TokenCounter（集成层适配器，结构实现端口协议）。

    `count_messages_tokens` 口径：每条消息 +4（格式开销）+ content token 数
    + name 额外 +1；末尾 +2（回复格式开销）。输出余量（max_tokens）属 TPM
    限流特有口径，不在端口方法内。
    """

    def __init__(self, model: str) -> None:
        self._encoder = get_encoder(model)

    def count_tokens(self, text: str) -> int:
        """计算单段文本的 token 数。"""
        return len(self._encoder.encode(text))

    def count_messages_tokens(self, messages: Messages) -> int:
        """计算 messages 列表的总 token 数（含格式开销，content 防御归一化）。"""
        total = 0
        for msg in messages:
            total += 4  # 每条消息的格式开销
            total += self.count_tokens(content_to_text(msg.get("content")))
            if msg.get("name"):
                total += 1
        total += 2  # 回复格式开销
        return total
