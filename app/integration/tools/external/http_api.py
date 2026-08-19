"""
外部工具示例：HTTP API 调用工具（HttpApiTool）

本文件是「外部工具热加载」的示例实现 —— 放入 external/ 目录即被
ExternalToolLoader 自动发现，下一次工具调用生效（无需重启），且自动获得
executor 全量横切关注点（参数校验 / 超时 / 重试 / 截断 / 审计 / 并发 / 审批）。

本示例刻意演示 BaseTool 的完整能力：
- 元数据覆写：risk_level（L1 写，因支持 POST/PUT/DELETE 改外部状态）、
  category（http）、concurrency_safe、timeout（15s）
- 生命周期钩子：on_load 建立 httpx 连接池 / on_unload 释放（资源可完整回收）
- 参数 schema：enum（method）+ required（url）+ 可选 headers/body
- 业务错误走返回值（ToolResult），不让异常抛出；异常分类归因
- **配置注入**：模块级 `CONFIG_KEYS` 声明 settings 配置键，loader 经 config_source
  注入 `register_config`（对齐内置工具风格，不硬编码——见 TOOLS-010）

与内置 web_browse（HTML → 文本）互补：本工具直接调用 REST API 拿 JSON。
"""

from typing import Any, ClassVar

import httpx

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.security import RiskLevel

# 配置注入契约：声明需要的 settings 键，loader 从装配根绑定的 config_source 取值注入
CONFIG_KEYS = ("tool_http_timeout",)

# settings 未配置时的默认值（不硬编码运行时配置，默认值兜底）
_DEFAULT_TIMEOUT = 15.0


class HttpApiTool(BaseTool):
    """发送 HTTP 请求并返回响应（REST API / 天气 / 汇率 / 内部服务等 JSON 接口）。"""

    _client_timeout: ClassVar[float] = _DEFAULT_TIMEOUT

    @classmethod
    def register_config(
        cls, *, tool_http_timeout: float | None = None, **kwargs: Any
    ) -> None:
        """注入请求超时配置（由 ExternalToolLoader 加载时调用，对齐内置工具 register_config 风格）。"""
        if tool_http_timeout is not None:
            cls._client_timeout = tool_http_timeout

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    # ===== 生命周期钩子（资源建立 / 释放） =====

    async def on_load(self) -> None:
        """加载后建立全局复用连接池（on_load 由外部工具加载器调用）。"""
        self._ensure_client()

    async def on_unload(self) -> None:
        """卸载前关闭连接池（资源完整回收，防连接泄漏）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        """获取连接池；未初始化则惰性创建。

        返回非 None（类型收窄），on_load 与 execute 兜底共用单一创建点。
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._client_timeout,
                follow_redirects=True,
                max_redirects=5,
            )
        return self._client

    # ===== 元数据（分级标注 + 调度声明） =====

    @property
    def name(self) -> str:
        return "http_api"

    @property
    def description(self) -> str:
        return (
            "发送 HTTP 请求到指定 URL 并返回响应内容。"
            "适用于调用 REST API、查询天气预报 / 汇率等公开接口、访问内部服务。"
            "GET 为只读查询；POST / PUT / DELETE 会对外部系统产生副作用。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE"],
                    "description": "HTTP 方法（默认 GET）",
                },
                "url": {
                    "type": "string",
                    "description": "请求目标 URL（完整地址，如 https://api.example.com/data）",
                },
                "headers": {
                    "type": "object",
                    "description": '请求头（键值对，如 {"Authorization": "Bearer xxx"}，可选）',
                },
                "body": {
                    "type": "object",
                    "description": "请求体（JSON 对象，POST / PUT 使用，可选）",
                },
            },
            "required": ["url"],
        }

    @property
    def risk_level(self) -> RiskLevel:
        """支持 POST/PUT/DELETE 改外部状态，L1 写。"""
        return RiskLevel.L1_WRITE

    @property
    def category(self) -> str:
        return "http"

    @property
    def timeout(self) -> int:
        """工具自声明默认超时（秒），与内部连接层超时一致。"""
        return 15

    # ===== 执行 =====

    async def execute(self, **kwargs) -> ToolResult:
        """发送 HTTP 请求（业务错误走返回值，不让异常抛出）。"""
        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")

        method = str(kwargs.get("method", "GET")).upper()
        url: str = kwargs["url"]
        headers = kwargs.get("headers") or None
        body = kwargs.get("body")

        # 连接池：未加载（直接实例化场景）时惰性创建，类型已收窄为非 None
        client = self._ensure_client()

        try:
            resp = await client.request(method, url, headers=headers, json=body)
            metadata = {
                "status_code": resp.status_code,
                "method": method,
                "url": url,
                "content_type": resp.headers.get("content-type", ""),
            }
            if resp.status_code >= 400:
                return ToolResult(
                    success=False,
                    content=resp.text,  # 4xx/5xx 响应体给 LLM 看错误详情
                    error=f"HTTP {resp.status_code}",
                    metadata=metadata,
                )
            return ToolResult(success=True, content=resp.text, metadata=metadata)
        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                content="",
                error=f"请求超时（{self._client_timeout:.0f} 秒）: {url}",
            )
        except httpx.RequestError as e:
            return ToolResult(success=False, content="", error=f"请求失败: {e!s}")
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, content="", error=f"HTTP 调用失败: {e!s}")
