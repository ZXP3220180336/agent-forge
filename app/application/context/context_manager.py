"""
上下文管理器
- 负责从会话历史中组装 messages
- 经 LLMGateway 端口（count_tokens/count_messages_tokens）自动进行 Token 计数和截断
- 支持历史摘要压缩
- 结构实现 ContextBudgetPort：Agent 运行中上下文预算管理（横切能力，注入 Agent 共享）
"""

from app.application.session.session_manager import SessionManager
from app.domain.ports.llm_gateway import LLMGateway
from app.shared.types import SessionId


class ContextManager:
    """
    上下文管理模块是整个多轮对话系统的核心调度器，它负责：
    1. 消息组装：从会话历史中提取消息，拼接成 LLM 可接受的格式
    2. Token 精确控制：经 LLMGateway 端口（LLMService 实现）计算每条消息和总上下文的 Token 消耗，确保不超过模型限制
    3. 窗口管理：当上下文超出限制时，自动截断或压缩历史
    4. 成本核算：为每次请求提供 Token 消耗数据，用于计费和监控
    """

    def __init__(
        self,
        session_manager: SessionManager,
        llm: LLMGateway,
        max_context_tokens: int = 128000,
        max_output_tokens: int = 4096,
    ):
        self.session_manager = session_manager
        self.llm = llm
        self.max_context_tokens = max_context_tokens
        self.max_output_tokens = max_output_tokens

    def count_tokens(self, text: str) -> int:
        """精确计算 Token 数量（委托 LLMGateway.count_tokens）"""
        return self.llm.count_tokens(text)

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """计算 messages 列表的总 Token 数（委托 LLMGateway.count_messages_tokens）"""
        return self.llm.count_messages_tokens(messages)

    async def build_messages(
        self,
        session_id: SessionId,
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

    # ==================================================================
    # Agent 运行中上下文预算管理（结构实现 ContextBudgetPort，横切能力）
    # ==================================================================
    # 输入侧（build_messages）负责初始组装截断；此处负责 Agent 循环中
    # （模型每次调用前）的增量护栏——所有 Agent 模式经端口注入共享。

    def trim_messages(
        self,
        messages: list[dict],
        *,
        max_rounds: int | None,
        max_tokens: int | None,
    ) -> None:
        """Agent 运行中上下文预算管理：轮次 + token 双层护栏（就地裁剪 messages）。

        轮次预算保证消息数有界（配对原子保留），token 预算保证总量不超上下文窗口
        （token 超限时逐轮丢最旧 assistant/tool 对）。保留 system/user 前缀。

        Args:
            messages: Agent 循环中可变消息列表（就地修改）
            max_rounds: 保留最大轮数（None=不限）
            max_tokens: 消息总 token 上限（None=不限）
        """
        if max_rounds is not None:
            self._trim_to_recent_rounds(messages, max_rounds)
        if max_tokens is not None:
            self._trim_to_token_budget(messages, max_tokens)

    def _trim_to_recent_rounds(self, messages: list[dict], max_rounds: int) -> None:
        """轮次滑动窗口：保留 system/user 前缀 + 最近 max_rounds 轮 assistant/tool 配对。"""
        keep: list[dict] = []
        idx = 0
        while idx < len(messages) and messages[idx].get("role") != "assistant":
            keep.append(messages[idx])
            idx += 1
        tail = messages[idx:]
        if sum(1 for m in tail if m.get("role") == "assistant") <= max_rounds:
            return
        seen = 0
        start = 0
        for i in range(len(tail) - 1, -1, -1):
            if tail[i].get("role") == "assistant":
                seen += 1
                if seen == max_rounds:
                    start = i
                    break
        messages[:] = keep + tail[start:]

    def _estimate_messages_tokens(self, messages: list[dict]) -> int:
        """估算总 token：count_messages_tokens 补 tool_calls/reasoning_content 低估。"""
        total = self.count_messages_tokens(messages)
        extra = 0
        for m in messages:
            for tc in m.get("tool_calls") or []:
                extra += len(tc.get("function", {}).get("arguments", "")) // 4
            extra += len(m.get("reasoning_content") or "") // 4
        return total + extra

    def _trim_to_token_budget(self, messages: list[dict], max_tokens: int) -> None:
        """token 超预算时逐轮丢最旧 assistant/tool 对，直到预算内（保留前缀）。"""
        while self._estimate_messages_tokens(messages) > max_tokens:
            first = next(
                (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
                None,
            )
            if first is None:
                break
            end = first + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            del messages[first:end]
