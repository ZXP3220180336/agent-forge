"""
HttpApiTool 单元测试（外部工具示例）

覆盖：
    参数 schema（url 必填 / method 枚举）
    GET 成功 / POST 带 body / 4xx 失败 / 连接错误 / 超时 / 校验失败
    生命周期钩子 on_load 建连接池 / on_unload 释放
    元数据（L1 写 / category=http / concurrency_safe / timeout=15）

全部经 httpx.MockTransport 模拟 HTTP，离线安全。
"""

import httpx
import pytest

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.external.http_api import HttpApiTool
from app.integration.tools.security import RiskLevel


def _inject_client(tool: HttpApiTool, handler) -> None:
    """注入 MockTransport client（模拟 HTTP，不真实联网）。"""
    tool._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(5.0),
        follow_redirects=True,
    )


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "path": request.url.path, "method": request.method},
    )


def _not_found_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"error": "not found"})


@pytest.mark.asyncio
async def test_schema_url_required_and_method_enum():
    """参数 schema：url 必填，method 限定枚举。"""
    params = HttpApiTool().parameters
    assert "url" in params["required"]
    assert params["properties"]["method"]["enum"] == ["GET", "POST", "PUT", "DELETE"]
    assert params["properties"]["method"].get("default") is None  # 默认值在 execute 内处理


@pytest.mark.asyncio
async def test_get_success():
    """GET 成功：success=True，content 含 JSON，metadata 记录状态码与方法。"""
    tool = HttpApiTool()
    _inject_client(tool, _ok_handler)

    result = await tool.execute(url="https://api.example.com/data")

    assert result.success is True
    assert '"ok":true' in result.content
    assert result.metadata["status_code"] == 200
    assert result.metadata["method"] == "GET"
    await tool.on_unload()


@pytest.mark.asyncio
async def test_post_with_body_and_headers():
    """POST + body + headers：请求透传（handler 校验收到 JSON 体）。"""
    captured: dict = {}

    def _post_handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["json"] = request.content
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"created": True})

    tool = HttpApiTool()
    _inject_client(tool, _post_handler)

    result = await tool.execute(
        method="POST",
        url="https://api.example.com/items",
        headers={"Authorization": "Bearer tok"},
        body={"name": "demo"},
    )

    assert result.success is True
    assert captured["method"] == "POST"
    assert b'"name":"demo"' in captured["json"]
    assert captured["auth"] == "Bearer tok"
    assert result.metadata["status_code"] == 201
    await tool.on_unload()


@pytest.mark.asyncio
async def test_http_error_returns_failure():
    """4xx：success=False，error 含状态码，content 保留响应体供 LLM 查看。"""
    tool = HttpApiTool()
    _inject_client(tool, _not_found_handler)

    result = await tool.execute(url="https://api.example.com/missing")

    assert result.success is False
    assert result.error == "HTTP 404"
    assert "not found" in result.content
    await tool.on_unload()


@pytest.mark.asyncio
async def test_request_error_returns_failure():
    """连接错误：归因为「请求失败」。"""

    def _boom_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    tool = HttpApiTool()
    _inject_client(tool, _boom_handler)

    result = await tool.execute(url="https://unreachable.example.com")

    assert result.success is False
    assert "请求失败" in result.error
    await tool.on_unload()


@pytest.mark.asyncio
async def test_timeout_returns_failure():
    """超时：request 抛 TimeoutException → 归因为「请求超时」。"""

    class _TimeoutClient:
        async def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("timed out", request=None)

        async def aclose(self) -> None:
            pass

    tool = HttpApiTool()
    tool._client = _TimeoutClient()  # MockTransport 不套用读超时，用 fake client 稳定触发

    result = await tool.execute(url="https://api.example.com/slow")

    assert result.success is False
    assert "请求超时" in result.error
    await tool.on_unload()


@pytest.mark.asyncio
async def test_validation_failure():
    """缺必填 url → 校验兜底失败。"""
    tool = HttpApiTool()
    _inject_client(tool, _ok_handler)

    result = await tool.execute(method="GET")

    assert result.success is False
    assert "参数有误" in result.error
    await tool.on_unload()


@pytest.mark.asyncio
async def test_metadata_declarations():
    """元数据：L1 写 / category=http / 并发安全 / 默认超时 15s / 写操作需审批。"""
    tool = HttpApiTool()
    assert tool.risk_level == RiskLevel.L1_WRITE
    assert tool.category == "http"
    assert tool.concurrency_safe is True
    assert tool.timeout == 15
    assert tool.max_output_length == 100_000
    assert tool.requires_approval is True  # 写方法工具一律需人工审批确认


@pytest.mark.asyncio
async def test_on_load_creates_client_and_on_unload_closes():
    """生命周期钩子：on_load 建立连接池，on_unload 关闭。"""
    tool = HttpApiTool()
    assert tool._client is None

    await tool.on_load()
    assert tool._client is not None

    await tool.on_unload()
    assert tool._client is None


@pytest.mark.asyncio
async def test_lazy_client_when_no_on_load():
    """直接实例化未走 loader：execute 惰性建连接池后仍可执行。"""
    tool = HttpApiTool()  # 未调用 on_load
    _inject_client(tool, _ok_handler)

    result = await tool.execute(url="https://api.example.com/data")

    assert result.success is True
    assert result.content != ""
    await tool.on_unload()


@pytest.mark.asyncio
async def test_register_config_injects_timeout():
    """register_config 注入请求超时（loader 配置注入路径，移除硬编码 _CLIENT_TIMEOUT）。"""
    HttpApiTool.register_config(tool_http_timeout=25.0)
    tool = HttpApiTool()
    try:
        assert tool._client_timeout == 25.0  # 类级配置被注入
        client = tool._ensure_client()
        assert client.timeout.connect == 25.0  # 连接层超时使用注入值
    finally:
        await tool.on_unload()
        HttpApiTool.register_config(tool_http_timeout=15.0)  # 复位默认


@pytest.mark.asyncio
async def test_ssrf_blocks_internal_url():
    """裸 IP / 内网 URL 被 SSRF 防护拦截（不发起请求）。"""
    tool = HttpApiTool()
    await tool.on_load()  # 建真实 client（含 SSRF event_hooks）
    try:
        result = await tool.execute(url="http://127.0.0.1:8000/api")
    finally:
        await tool.on_unload()

    assert result.success is False
    assert "SSRF" in result.error
    assert "裸 IP" in result.error
