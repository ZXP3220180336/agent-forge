"""LLM 请求级上下文预算闸。

Provider 网络调用前，依据最终请求参数与模型窗口配置拒绝无法容纳的请求；
不裁剪会话历史——多轮历史的语义取舍归 domain `ContextManager`
（关联决策：adr/integration/llm/2026-09-06-request-context-budget.md，LLM-ADR-016）。

组件：
- ``RequestBudgetConfig``  单个 model_key 的窗口能力与保守余量
- ``RequestBudgetResult``  一次请求的客户端保守估算（供日志与测试读取）
- ``RequestBudgetGuard``   按最终请求参数校验，不保存请求级状态
- ``RequestBudgetManager`` 按 model_key 管理配置与 guard 缓存（register_config 注入，模式与限流配置管理器一致）

用法（装配根注入；默认值见 settings，缺失键走 ``_default_config``）::

    RequestBudgetManager.register_config({
        "main":      RequestBudgetConfig(context_window_tokens=128_000, safety_margin_tokens=1_024),
        "reasoning": RequestBudgetConfig(context_window_tokens=128_000, safety_margin_tokens=1_024),
    })
    guard = RequestBudgetManager.get("main")
    guard.validate(model, request)  # 超限抛 ContextWindowExceededError，不会调用 provider
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar

from app.shared.exceptions import (
    ContextWindowExceededError,
    ParameterValidationError,
)

from .token_counter import get_encoder

# 计入上下文窗口的内容字段白名单（ADR-016 Decision 4：messages / tool definitions /
# response format + 固定协议开销）。sampling（temperature/seed 等）、metadata 等控制与
# 追踪参数不占模型输入窗口，不参与计数；反之未知内容字段宁可漏计也不臆造窗口消耗。
# 模块级常量用普通类型注解（ClassVar 仅用于类体标注类变量）。
_INPUT_CONTENT_KEYS: frozenset[str] = frozenset({"messages", "tools", "response_format"})
# 请求外壳保守预留：Chat Completions 的字段/角色编码随 provider 变化，
# 不把 tiktoken 估算伪装成 provider 的精确计量。
_PROTOCOL_OVERHEAD_TOKENS: int = 16


def _estimate_payload_tokens(model: str, request: dict[str, Any]) -> int:
    """对最终请求中会进模型输入的内容字段做稳定 JSON 序列化后的保守估算。"""
    payload = {key: request[key] for key in _INPUT_CONTENT_KEYS if key in request}
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    # disallowed_special=()：用户文本可能含特殊 token 拼写（如 "<|endoftext|>"），
    # 预算估算只求数量级、不得因文本特殊拼写崩溃，按普通文本计数即可。
    tokens = len(get_encoder(model).encode(serialized, disallowed_special=()))
    return tokens + _PROTOCOL_OVERHEAD_TOKENS


@dataclass(frozen=True)
class RequestBudgetConfig:
    """单个 model_key 的窗口能力与保守余量。"""

    context_window_tokens: int
    safety_margin_tokens: int

    def __post_init__(self) -> None:
        window = self.context_window_tokens
        margin = self.safety_margin_tokens
        if not (
            isinstance(window, int)
            and not isinstance(window, bool)
            and window > 0
            and isinstance(margin, int)
            and not isinstance(margin, bool)
            and margin >= 0
            and margin < window
        ):
            raise ParameterValidationError(
                f"无效请求预算配置: window={window}, margin={margin}（需 window>0、margin∈[0, window)）"
            )


@dataclass(frozen=True)
class RequestBudgetResult:
    """一次请求的客户端保守估算，供日志与测试读取。"""

    input_tokens: int
    input_budget: int
    max_tokens: int


class RequestBudgetGuard:
    """按最终请求参数验证输入预算，不保存请求级状态。

    模型键由构造时绑定；可用输入额度 = 窗口 - 输出预留 - 安全余量。
    """

    def __init__(self, model_key: str, config: RequestBudgetConfig) -> None:
        self._model_key = model_key
        self._config = config

    def validate(self, model: str, request: dict[str, Any]) -> RequestBudgetResult:
        """校验一次最终请求是否可容纳于模型窗口。

        Args:
            model: 实际调用的模型名（用于解析编码器估算）。
            request: 最终 chat.completions 请求参数（含 messages/tools/
                response_format/max_tokens）。

        Returns:
            本次请求的客户端估算（input_tokens / input_budget / max_tokens）。

        Raises:
            ParameterValidationError: max_tokens 不是正整数。
            ContextWindowExceededError: 输入估算超出可用额度；在网络调用前拒绝，
                调用方不得把它当作可重试网络故障或空输出处理。
        """
        max_tokens = request.get("max_tokens")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            raise ParameterValidationError(f"max_tokens 必须为正整数，收到 {max_tokens!r}")
        input_budget = self._config.context_window_tokens - self._config.safety_margin_tokens - max_tokens
        input_tokens = _estimate_payload_tokens(model, request)
        result = RequestBudgetResult(input_tokens, input_budget, max_tokens)
        if input_budget < 0 or input_tokens > input_budget:
            raise ContextWindowExceededError(
                model_key=self._model_key,
                input_tokens=input_tokens,
                input_budget=input_budget,
                max_tokens=max_tokens,
            )
        return result


class RequestBudgetManager:
    """按 model_key 管理窗口配置与 guard 缓存（模式与限流配置管理器一致）。"""

    _default_config: ClassVar[RequestBudgetConfig] = RequestBudgetConfig(
        context_window_tokens=128_000, safety_margin_tokens=1_024
    )
    _configs: ClassVar[dict[str, RequestBudgetConfig]] = {}
    _instances: ClassVar[dict[str, RequestBudgetGuard]] = {}

    @classmethod
    def register_config(cls, configs: dict[str, RequestBudgetConfig]) -> None:
        """整体替换按 model_key 索引的运行期配置，并重建实例缓存。

        Args:
            configs: 各 model_key 的窗口配置（缺失键在 get 时走默认配置）。
        """
        cls._configs = dict(configs)
        cls.reset()

    @classmethod
    def reset(cls) -> None:
        """清空按键缓存的 guard，保留已注入配置（register_config 与测试隔离复用）。"""
        cls._instances = {}

    @classmethod
    def get(cls, model_key: str = "main") -> RequestBudgetGuard:
        """获取按键缓存的 guard；缺失键使用默认配置。

        Args:
            model_key: 配置键（main / reasoning / fast 等）。

        Returns:
            绑定了对应窗口配置的 RequestBudgetGuard（懒建并缓存）。
        """
        if model_key in cls._instances:
            return cls._instances[model_key]
        config = cls._configs.get(model_key, cls._default_config)
        guard = RequestBudgetGuard(model_key, config)
        cls._instances[model_key] = guard
        return guard
