"""
ClientManager — 连接池复用与多 client 管理

职责：
    1. 全局共享 AsyncOpenAI client 实例，避免每次请求创建新连接
    2. 支持三种预配置 client：main / reasoning / fast，按需获取
    3. 支持 HTTP 代理

使用方式：
    client = ClientManager.get_client("reasoning")
    response = await client.chat.completions.create(...)

    # 自定义配置
    custom = ClientManager.get_client("custom", {
        "api_key": "...", "base_url": "...", "model": "..."
    })
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from openai import AsyncOpenAI

# AsyncOpenAI 构造函数支持的参数（排除内部管理字段）
_OPENAI_CLIENT_KWARGS = {
    "api_key",
    "organization",
    "base_url",
    "timeout",
    "max_retries",
    "default_headers",
    "default_query",
    "http_client",
    "websocket_client",
}


class ClientManager:
    """
    连接池管理器。

    全局共享 client 实例，避免重复创建连接和 SSL 握手。
    key 约定："main" / "reasoning" / "fast" 对应配置中心三种模型。
    """

    _instances: ClassVar[dict[str, AsyncOpenAI]] = {}
    _configs: ClassVar[dict[str, dict[str, Any]]] = {}

    @classmethod
    def register_config(
        cls,
        key: str,
        api_key: str,
        base_url: str,
        model: str,
        **extra: Any,
    ) -> None:
        """
        注册一个 client 配置（模型名、密钥、端点）。

        配置注册后不会立即创建 client，第一次 get_client 时懒加载。
        """
        cls._configs[key] = {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            **extra,
        }
        # 清除旧实例，下次 get_client 时重建
        old = cls._instances.pop(key, None)
        if old is not None:
            try:
                asyncio.ensure_future(old.close())
            except RuntimeError:
                pass  # 无事件循环时忽略（纯注册阶段）

    @classmethod
    def get_client(cls, key: str = "main") -> AsyncOpenAI:
        """
        获取或创建指定 key 的 AsyncOpenAI client。

        Args:
            key: 配置标识，默认 "main"

        Returns:
            AsyncOpenAI 实例

        Raises:
            ValueError: key 未注册
        """
        if key not in cls._instances:
            if key not in cls._configs:
                raise ValueError(
                    f"Client key {key!r} 未注册。请先调用 register_config()。"
                )
            cfg = cls._configs[key]
            client_kwargs: dict[str, Any] = {
                k: cfg[k] for k in _OPENAI_CLIENT_KWARGS if k in cfg
            }
            # 默认值兜底
            client_kwargs.setdefault("api_key", "")
            client_kwargs.setdefault("base_url", "https://api.openai.com/v1")
            # 可选代理
            proxy_url = cfg.get("proxy_url")
            if proxy_url:
                client_kwargs["http_client"] = _build_proxied_client(proxy_url)
            cls._instances[key] = AsyncOpenAI(**client_kwargs)
        return cls._instances[key]

    @classmethod
    def get_model(cls, key: str = "main") -> str:
        """获取指定 key 的模型名。"""
        if key not in cls._configs:
            raise ValueError(f"Client key {key!r} 未注册。")
        return cls._configs[key]["model"]

    @classmethod
    def get_config(cls, key: str = "main") -> dict[str, Any]:
        """获取指定 key 的完整配置副本。"""
        if key not in cls._configs:
            raise ValueError(f"Client key {key!r} 未注册。")
        return dict(cls._configs[key])

    @classmethod
    def list_keys(cls) -> list[str]:
        """列出所有已注册的 key。"""
        return list(cls._configs.keys())

    @classmethod
    async def close_all(cls) -> None:
        """清理所有缓存的 client 实例（关闭底层连接池）。"""
        for client in cls._instances.values():
            await client.close()
        cls._instances.clear()

    @classmethod
    async def close_client(cls, key: str) -> None:
        """关闭并移除指定 key 的 client。"""
        client = cls._instances.pop(key, None)
        if client is not None:
            await client.close()

    @classmethod
    def remove(cls, key: str) -> None:
        """移除指定配置和 client 实例（不关闭连接，仅清理引用）。"""
        cls._instances.pop(key, None)
        cls._configs.pop(key, None)


def _build_proxied_client(proxy_url: str) -> Any:
    """构建带代理的 httpx.AsyncClient（延迟导入避免硬依赖）。"""
    try:
        import httpx
    except ImportError:
        raise ImportError("使用代理需要安装 httpx: pip install httpx")
    return httpx.AsyncClient(proxy=proxy_url)
