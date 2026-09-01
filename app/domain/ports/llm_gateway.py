"""LLM 网关端口（领域层拥有的抽象契约）。

依赖倒置：领域层 Agent 依赖本协议，能力层 LLMService 结构实现之。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Protocol, runtime_checkable

from app.shared.types import Messages


class StreamResult:
    """LLM 单轮流式生成的结果载体（领域契约）。"""

    def __init__(self) -> None:
        self.content: str = ""
        self.reasoning_content: str = ""
        # 标记模型响应是否出现了 reasoning_content 字段（含空串）——DeepSeek V4
        # thinking 模式带 tools 时必须回喂 reasoning_content（否则 400），空 reasoning
        # 也需回喂空串；本标记区分「未返回」与「返回空」，供编排层决策回喂。
        self.has_reasoning: bool = False
        self.finish_reason: str | None = None
        self.tool_calls: list[dict] = []
        self.usage: dict | None = None
        self.refusal: str | None = None
        # LLM 调用失败原因（create 失败 / 流中断放弃 / 用户取消），None=成功。
        # 失败信号供编排层（Agent）短路决策——避免把「LLM 失败」当「空输出」空转重试。
        # 正常空回（stop + 空 content）不置位；整流成功路径不置位（只有最终放弃才置）。
        self.error: str | None = None


@runtime_checkable
class LLMGateway(Protocol):
    """LLM 网关抽象：领域层对模型调用的唯一依赖面。"""

    async def async_generate(
        self,
        messages: Messages,
        tools: list[dict] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        result: StreamResult | None = None,
        model_key: str = "main",
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[str]:
        """流式生成；yield 标记为 async generator（类型用途，运行时不可达）。"""
        yield ""  # 使类型检查器识别为 async generator，可被 async for 遍历

    async def generate(
        self,
        messages: Messages,
        tools: list[dict] | None = None,
        temperature: float = 0,
        max_tokens: int = 1024,
        response_format: dict | None = None,
        model_key: str = "fast",
    ) -> StreamResult | None: ...

    async def generate_structured(
        self,
        messages: Messages,
        schema: dict[str, Any],
        model_key: str = "fast",
        max_tokens: int | None = None,
        usage: dict | None = None,
    ) -> dict | None: ...

    def calculate_cost(
        self,
        usage: dict[str, Any] | None,
        model: str = "",
    ) -> dict[str, float]:
        """按 model 定价折算 usage 成本明细（同步纯函数）。

        成本估算是 LLM 能力（模型定价）——归属 LLMGateway，供成本上限等
        横切护栏经同一端口接入（应用层 CostLimiter 依赖本端口，不触及
        集成层子组件 CostTracker）。实现方 LLMService.calculate_cost 为
        静态方法代理 CostTracker。

        Args:
            usage: token 用量（跨轮累计）{"prompt_tokens", "completion_tokens",
                "total_tokens"}；None/空 → 全 0。
            model: 定价查找键；空串走默认均价兜底。

        Returns:
            {"cost_usd": float, "input_cost": float, "output_cost": float}（round 6）。
        """
        ...

    def count_tokens(self, text: str) -> int:
        """计算单段文本的 token 数（模型编码，同步纯函数）。

        Token 计数是 LLM 能力（模型特定编码）——归属 LLMGateway，供应用层
        ContextManager 等经同一端口接入，不触及集成层子组件 tiktoken。
        实现方 LLMService 委托 tiktoken 编码器（主模型编码）。
        """
        ...

    def count_messages_tokens(self, messages: Messages) -> int:
        """计算 messages 列表的总 token 数（含每条消息格式开销 + 末尾回复开销）。

        口径与实现方一致：每条消息 +4 + content token 数 + name 额外 +1；末尾 +2。
        """
        ...
