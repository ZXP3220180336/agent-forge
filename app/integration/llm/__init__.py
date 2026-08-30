"""
LLM 包 — 工业级 LLM 通信组件（Facade 对外）

对外接口：仅 `LLMService`（实现领域端口 `LLMGateway`）。
内部组件（client / retry / reservation_limiter / streaming_rectifier /
structured / streaming / cost_tracker / token_counter）不对外暴露——消费方
（领域 / 应用 / API 层）只依赖 `LLMService`；装配根（container）经深路径
import 内部组件做 register_config 接线（组合根例外）。
"""

from .llm_service import LLMService

__all__ = ["LLMService"]
