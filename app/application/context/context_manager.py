"""
上下文管理器
- 负责从会话历史中组装 messages
- 自动进行 Token 计数和截断
- 支持历史摘要压缩
"""

import tiktoken

from .session_manager import SessionManager


class ContextManager:
    """
    上下文管理模块是整个多轮对话系统的核心调度器，它负责：
    1. 消息组装：从会话历史中提取消息，拼接成 LLM 可接受的格式
    2. Token 精确控制：计算每条消息和总上下文的 Token 消耗，确保不超过模型限制
    3. 窗口管理：当上下文超出限制时，自动截断或压缩历史
    4. 成本核算：为每次请求提供 Token 消耗数据，用于计费和监控
    """

    def __init__(
        self,
        session_manager: SessionManager,
        model_name: str = "gpt-4",
        max_context_tokens: int = 128000,
        max_output_tokens: int = 4096,
    ):
        self.session_manager = session_manager
        self.model_name = model_name
        self.max_context_tokens = max_context_tokens
        self.max_output_tokens = max_output_tokens

        # 使用 tiktoken 进行精确 Token 计数
        try:
            self.encoder = tiktoken.encoding_for_model(model_name)
        except KeyError:
            self.encoder = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text: str) -> int:
        """精确计算 Token 数量"""
        return len(self.encoder.encode(text))

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """计算 messages 列表的总 Token 数"""
        total = 0
        for msg in messages:
            total += 4  # 每条消息的格式开销
            total += self.count_tokens(msg.get("content", ""))
            if msg.get("name"):
                total += 1
        total += 2  # 回复格式开销
        return total

    async def build_messages(
        self,
        session_id: str,
        user_message: str,
        max_rounds: int = 20,
    ) -> tuple[list[dict], int]:
        """
        构建发送给 LLM 的 messages

        策略：
        1. 始终保留 system prompt
        2. 保留最近的 N 轮对话（max_rounds 控制）
        3. 如果 Token 超出限制，从最早的对话开始丢弃
        4. 如果仍然超出，对历史进行摘要压缩

        Args:
            session_id: 会话ID
            user_message: 用户当前输入
            max_rounds: 保留的最大对话轮数

        Returns:
            (messages, total_tokens): 组装好的消息列表和Token总数
        """
        # 1. 获取会话信息（含 system prompt）
        session = await self.session_manager.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        # 2. 获取历史消息（最近 max_rounds 轮）
        history = await self.session_manager.get_messages(
            session_id,
            limit=max_rounds * 2,  # 每轮 user + assistant
        )

        # 3. 组装 messages
        messages = [{"role": "system", "content": session["system_prompt"]}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # 4. 计算 Token 并截断
        total_tokens = self.count_messages_tokens(messages)
        available_tokens = self.max_context_tokens - self.max_output_tokens

        if total_tokens > available_tokens:
            messages = self._truncate_messages(messages, available_tokens)
            total_tokens = self.count_messages_tokens(messages)

        return messages, total_tokens

    # 当前策略：从最早的消息开始丢弃
    # 问题：如果早期消息包含关键信息，被丢弃后模型无法理解上下文
    def _truncate_messages(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> list[dict]:
        """
        截断消息，保留 system prompt 和最近的对话

        策略：
        - 保留 system prompt（索引0）
        - 从最早的历史消息开始丢弃
        - 直到 Token 总数低于限制
        """
        # 保留 system prompt
        truncated = [messages[0]]

        # 从最近的开始保留
        for msg in reversed(messages[1:-1]):  # 去掉 system 和最后的 user
            candidate = [msg] + truncated[1:]
            candidate = [truncated[0]] + candidate  # 加上 system prompt
            candidate = candidate + [messages[-1]]  # 加上 user prompt
            if self.count_messages_tokens(candidate) <= max_tokens:
                truncated.insert(1, msg)
            else:
                break

        # 加上最后的 user 消息
        truncated.append(messages[-1])

        return truncated
