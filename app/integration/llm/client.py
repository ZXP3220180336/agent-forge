"""
ClientManager — 连接池复用与多 client 管理

职责：
    1. 全局共享 AsyncOpenAI client 实例，避免每次请求创建新连接
    2. 支持三种预配置 client：main / reasoning / fast，按需获取
    3. 支持 HTTP 代理

使用方式：
    client.register_config("reasoning", api_key="...", base_url="...", model="...")
    client = ClientManager.get_client("reasoning")
    response = await client.chat.completions.create(...)

    # 自定义配置
    ClientManager.register_config("custom", api_key="...", base_url="...", model="...")
    custom = ClientManager.get_client("custom")
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from openai import AsyncOpenAI

from app.utils.logger import get_logger

logger = get_logger("llm.client")

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
    # 待关闭的旧 client（无事件循环时 register_config 无法 fire-and-forget，
    # 放入此列表由 close_all 统一关闭，避免旧连接池泄漏且可追踪）
    _pending_closes: ClassVar[list[AsyncOpenAI]] = []
    # 有事件循环时后台关闭旧 client 的 task（close_all 等待其完成，避免
    # task 泄漏、竞态与 "Task was destroyed but it is pending" 警告）。
    # 完成回调 _on_closing_task_done 自动移除已完成的 task + 消费异常记日志
    # （不依赖 close_all 显式检查，热切换后台关闭失败也不静默）。
    _closing_tasks: ClassVar[list[asyncio.Task]] = []

    @classmethod
    def _on_closing_task_done(cls, task: asyncio.Task) -> None:
        """后台关闭 task 完成回调：移除引用 + 消费异常（不静默、不泄漏）。

        - 从 _closing_tasks 移除已完成 task：多次热切换（register_config 反复
          注册）后列表不累积已完成引用，避免内存泄漏与事件循环关闭时
          "Task was destroyed but it is pending" 警告。
        - 消费 task 异常并记日志：后台关闭失败不静默消失——与 close_all 对
          _instances/_pending_closes 逐 client 关闭失败记日志的处理对称。
        """
        if task in cls._closing_tasks:
            cls._closing_tasks.remove(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("后台关闭旧 LLM client 失败", exc_info=exc)

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
                # 仅在有运行中事件循环时后台关闭（fire-and-forget，不阻塞注册）
                asyncio.get_running_loop()
            except RuntimeError:
                # 无运行事件循环（纯注册阶段，如 Container.initialize 之前）：
                # 无法后台关闭，放入待关闭列表由 close_all 统一关闭，
                # 避免旧连接池泄漏且可追踪（不再静默忽略）。
                cls._pending_closes.append(old)
            else:
                # 后台关闭 task 记录到 _closing_tasks，由 close_all 统一等待，
                # 避免 task 泄漏（无引用）与事件循环先关闭时 pending task 警告。
                # 挂完成回调：task 结束后自动从列表移除（热切换不累积）+
                # 消费异常记日志（后台关闭失败不静默）。
                task = asyncio.ensure_future(old.close())
                task.add_done_callback(cls._on_closing_task_done)
                cls._closing_tasks.append(task)

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
        """清理所有缓存的 client 实例（关闭底层连接池）。

        先快照再逐个关闭：迭代期间 await client.close() 会让出事件循环控制权，
        若其他协程 register_config()/close_client() 修改字典，直接迭代会抛
        RuntimeError。单个 close 异常用日志隔离，不中断其余 client 的关闭。
        """
        # 先等待后台 close task 完成（含热切换产生的旧 client 后台关闭），
        # 避免与 close_all 并行关闭的竞态、task 泄漏与异常无人消费。
        if cls._closing_tasks:
            await asyncio.gather(*cls._closing_tasks, return_exceptions=True)
            cls._closing_tasks.clear()

        for client in list(cls._instances.values()):
            try:
                await client.close()
            except Exception:
                logger.warning("关闭 LLM client 失败", exc_info=True)
        cls._instances.clear()
        # 关闭无循环阶段积累的待关闭旧 client（register_config 无法 fire-and-forget）
        for client in list(cls._pending_closes):
            try:
                await client.close()
            except Exception:
                logger.warning("关闭待清理 LLM client 失败", exc_info=True)
        cls._pending_closes.clear()

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
