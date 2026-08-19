"""
网页浏览工具 - 获取并解析网页内容
"""

import html as html_module
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import urljoin

import httpx

from ..base import BaseTool, ToolResult
from ..security import RiskLevel, SSRFError, ssrf_on_request


class _HTMLToTextParser(HTMLParser):
    """HTML 转纯文本的解析器"""

    BLOCK_TAGS: ClassVar[set[str]] = {
        "p",
        "div",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "td",
        "th",
        "blockquote",
        "pre",
    }

    def __init__(self, base_url: str = ""):
        super().__init__()
        self.base_url = base_url
        self._text_parts: list[str] = []
        self._links: list[tuple[str, str]] = []  # (text, url)
        self._skip_tag = False  # 跳过 script/style 内容
        self._title = ""
        self._in_title = False
        self._in_pre = False
        self._in_link = 0  # <a> 嵌套计数：>0 表示文本处于链接内（`</a>` 后不误计入链接文本）
        self._last_was_block = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_tag = True
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            self._in_link += 1
            for name, val in attrs:
                if name == "href" and val and not val.startswith(("#", "javascript:")):
                    self._links.append(("", val))
            return
        if tag == "pre":
            self._in_pre = True
        if tag in self.BLOCK_TAGS and not self._last_was_block:
            self._text_parts.append("\n")
            self._last_was_block = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip_tag = False
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "a":
            self._in_link -= 1
            return
        if tag == "pre":
            self._in_pre = False
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"):
            self._text_parts.append("\n")
            self._last_was_block = True

    def _append_text(self, text: str) -> None:
        """追加文本：在链接内时更新链接显示文本，并联动块标记。

        handle_data 与 handle_entityref 共用——HTML 实体在链接内也能更新链接文本，
        且 `</a>` 后文本（_in_link=0）不误计入链接显示文本。
        """
        if self._in_link > 0 and self._links:
            last_text, last_url = self._links[-1]
            if not last_text:
                self._links[-1] = (text.strip()[:80], last_url)
        self._text_parts.append(text)
        self._last_was_block = False

    def handle_data(self, data):
        if self._skip_tag:
            return
        text = data.strip() if not self._in_pre else data
        if not text:
            return

        if self._in_title:
            self._title = text.strip()
            return

        self._append_text(text)

    def handle_entityref(self, name):
        if not self._skip_tag:
            # Python 3.9+ 移除了 HTMLParser.unescape，使用 html.unescape 替代
            char = html_module.unescape(f"&{name};")
            self._append_text(char)

    def get_text(self) -> str:
        """获取纯文本"""
        return "".join(self._text_parts).strip()

    def get_title(self) -> str:
        """获取页面标题"""
        return self._title or "(无标题)"

    def get_links_formatted(self, max_links: int = 20) -> str:
        """获取格式化链接列表"""
        seen: set[str] = set()
        lines = []
        for text, url in self._links:
            absolute_url = urljoin(self.base_url, url)
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            if text:
                lines.append(f"  - [{text}]({absolute_url})")
            else:
                lines.append(f"  - {absolute_url}")
        if not lines:
            return ""
        result = "\n".join(lines[:max_links])
        if len(lines) > max_links:
            result += f"\n  ...（还有 {len(lines) - max_links} 个链接）"
        return f"\n\n页面链接：\n{result}"


# 全局复用的 httpx 客户端（连接池复用）
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """获取全局 HTTP 客户端（单例，复用连接池）"""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=WebBrowseTool._max_redirects,
            timeout=WebBrowseTool._timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            event_hooks={"request": [ssrf_on_request]},
        )
    return _http_client


class WebBrowseTool(BaseTool):
    """网页浏览工具"""

    _max_content_length: ClassVar[int] = 50_000
    _timeout: ClassVar[float] = 15.0  # httpx 连接层超时（秒），经 register_config 注入
    _max_redirects: ClassVar[int] = 5  # 最大重定向数，经 register_config 注入

    @classmethod
    def register_config(
        cls,
        *,
        max_content_length: int = 50_000,
        timeout: float = 15.0,
        max_redirects: int = 5,
        **kwargs: Any,
    ) -> None:
        """注入网页内容截断 / 连接层超时 / 重定向配置（由装配根调用，避免直接依赖 settings）。"""
        cls._max_content_length = max_content_length
        cls._timeout = timeout
        cls._max_redirects = max_redirects

    async def on_unload(self) -> None:
        """关闭全局 httpx 连接池（内置工具随应用生命周期，由 ToolService.shutdown 调用）。

        幂等：client 已关闭则跳过，与外部工具的 on_unload 语义一致（资源完整回收）。
        """
        global _http_client
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None

    @property
    def risk_level(self) -> RiskLevel:
        """只读网页抓取，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "web"

    @property
    def timeout(self) -> int:
        """工具自声明默认超时（秒），与内部 httpx 超时（15s）一致。"""
        return 15

    @property
    def max_output_length(self) -> int:
        """结果截断上限（字符数），ResultProcessor 消费。"""
        return self._max_content_length

    @property
    def name(self) -> str:
        return "web_browse"

    @property
    def description(self) -> str:
        return (
            "获取指定 URL 的网页内容并返回纯文本版本。"
            "当你需要阅读网页文章、查看文档、或获取在线信息时使用此工具。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要访问的网页 URL（完整 URL，如 https://example.com/page）",
                },
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        """异步获取并解析网页内容"""

        if not self.validate_parameters(**kwargs):
            return self._invalid_params_result(**kwargs)

        url: str = kwargs["url"]

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        client = _get_http_client()

        try:
            # 流式读取响应体（限制内存峰值），HTML 解析器增量 feed；超限即停
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return ToolResult(
                        success=False,
                        content="",
                        error=f"HTTP {response.status_code}: {response.reason_phrase}",
                        metadata={"url": url, "status_code": response.status_code},
                    )

                content_type = response.headers.get("content-type", "")
                # 编码检测：Content-Type charset（charset_encoding）优先，缺失默认 utf-8；
                # 逐 chunk 按该编码解码（errors="replace" 防非法字节崩溃）
                encoding = response.charset_encoding or "utf-8"
                parser = _HTMLToTextParser(base_url=str(response.url))

                cap_bytes = self._max_content_length * 4  # UTF-8 中文 3B/字符 + 缓冲
                size = 0
                body_truncated = False
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > cap_bytes:
                        body_truncated = True
                        break
                    parser.feed(chunk.decode(encoding, errors="replace"))
                parser.close()

                page_text = parser.get_text()
                page_title = parser.get_title()
                links_block = parser.get_links_formatted()

                # 结果截断由 ResultProcessor 统一处理（head+tail）；body 过大时流式截断并标记
                content_parts = [
                    f"标题: {page_title}",
                    f"来源: {response.url}",
                    "",
                    page_text,
                ]
                if body_truncated:
                    content_parts.append("\n...（网页内容过大，仅保留前部）")
                if links_block:
                    content_parts.append(links_block)

                metadata: dict[str, Any] = {
                    "url": str(response.url),
                    "title": page_title,
                    "status_code": response.status_code,
                    "content_type": content_type,
                }
                if body_truncated:
                    metadata["truncated"] = True

                return ToolResult(
                    success=True,
                    content="\n".join(content_parts),
                    metadata=metadata,
                )

        except httpx.TimeoutException:
            return ToolResult(
                success=False,
                content="",
                error=f"请求超时（15 秒）: {url}",
            )
        except httpx.TooManyRedirects:
            return ToolResult(
                success=False,
                content="",
                error=f"重定向次数过多: {url}",
            )
        except SSRFError as e:
            return ToolResult(
                success=False,
                content="",
                error=f"请求被安全策略拦截（SSRF 防护）: {e!s}",
            )
        except httpx.RequestError as e:
            return ToolResult(
                success=False,
                content="",
                error=f"请求失败: {e!s}",
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                success=False,
                content="",
                error=f"页面解析失败: {e!s}",
            )
