"""
内置工具真实执行集成测试

用真实 ToolService + 内置工具验证端到端执行（不依赖网络）：
- writeFile / readFile：tmp_path 真实读写，含父目录自动创建与缺失文件错误
- code_exec：真实子进程执行安全命令 + 危险命令前缀拦截
- search：未配置 key 时的优雅失败（显式重置，避免读到 .env 真实 key 触发网络）
- init_default_tools 装配完整性（5 个内置工具）
"""

import asyncio
import locale
import sys

import httpx
import pytest

from app.domain.ports.tool_gateway import ErrorCode
from app.integration.tools.builtin import (
    CodeExecTool,
    ReadFileTool,
    SearchTool,
    WebBrowseTool,
    WriteFileTool,
    web_browse,
)
from app.integration.tools.builtin.code_exec import _decode_output
from app.integration.tools.tool_service import ToolService


@pytest.mark.asyncio
async def test_write_and_read_file_roundtrip(tmp_path):
    """writeFile → readFile 真实读写，父目录自动创建"""
    target = tmp_path / "nested" / "out.txt"
    WriteFileTool.register_config(allowed_dirs=(str(tmp_path),))
    ReadFileTool.register_config(allowed_dirs=(str(tmp_path),))
    service = ToolService()
    service.register(WriteFileTool())
    service.register(ReadFileTool())

    write_result = await service.execute(
        "writeFile",
        {"file_path": str(target), "content": "你好，世界"},
    )
    assert write_result.success is True
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "你好，世界"

    read_result = await service.execute("readFile", {"file_path": str(target)})
    assert read_result.success is True
    assert read_result.content == "你好，世界"


@pytest.mark.asyncio
async def test_read_file_missing_returns_error(tmp_path):
    ReadFileTool.register_config(allowed_dirs=(str(tmp_path),))
    service = ToolService()
    service.register(ReadFileTool())

    result = await service.execute("readFile", {"file_path": str(tmp_path / "no.txt")})

    assert result.success is False
    assert "未找到" in result.error


@pytest.mark.asyncio
async def test_code_exec_runs_python():
    """真实子进程执行 python 计算，返回标准输出"""
    service = ToolService()
    service.register(CodeExecTool())

    result = await service.execute(
        "code_exec",
        {"command": f'"{sys.executable}" -c "print(2+2)"'},
    )

    assert result.success is True
    assert "4" in result.content
    assert result.metadata["return_code"] == 0


@pytest.mark.asyncio
async def test_code_exec_rejects_forbidden_command():
    """危险命令前缀被安全策略拦截，不真正执行"""
    service = ToolService()
    service.register(CodeExecTool())

    result = await service.execute("code_exec", {"command": "rm -rf /"})

    assert result.success is False
    assert "安全策略" in result.error


@pytest.mark.asyncio
async def test_search_missing_key_graceful_failure():
    """未配置 TAVILY_API_KEY 时优雅失败，不发起网络请求"""
    SearchTool.register_config(api_key="", search_depth="basic")  # 显式清空，避免读 .env 真实 key
    service = ToolService()
    service.register(SearchTool())

    result = await service.execute("search", {"query": "测试"})

    assert result.success is False
    assert "TAVILY_API_KEY" in result.error


@pytest.mark.asyncio
async def test_init_default_tools_registers_all():
    """装配根一键注册 10 个内置工具（5 通用 + 5 RCA）"""
    service = ToolService()
    registered = service.init_default_tools()

    assert len(registered) == 10
    assert set(service.list_tools()) == {
        "search",
        "readFile",
        "writeFile",
        "code_exec",
        "web_browse",
        "query_batch_yield",
        "query_equipment_alerts",
        "query_fdc_params",
        "query_defect_map",
        "search_historical_rca",
    }


@pytest.mark.asyncio
async def test_read_file_large_content_truncated_head_tail(tmp_path):
    """readFile 大文件结果由 ResultProcessor 统一 head+tail 截断（含 marker）。"""
    target = tmp_path / "big.txt"
    # 超过 readFile 默认 max_output_length（100_000）
    content = ("头" * 500) + ("中" * 100_000) + ("尾" * 500)
    target.write_text(content, encoding="utf-8")

    ReadFileTool.register_config(allowed_dirs=(str(tmp_path),))
    service = ToolService()
    service.register(ReadFileTool())

    result = await service.execute("readFile", {"file_path": str(target)})

    assert result.success is True
    assert "已截断" in result.content
    assert "共 " in result.content  # marker 含原长度
    # 首尾保留（head 段 + tail 段）
    assert "头" * 500 in result.content
    assert "尾" * 500 in result.content
    assert result.metadata["truncated"] is True


@pytest.mark.asyncio
async def test_read_file_outside_allowed_dir_rejected(tmp_path):
    """白名单外路径被拒绝（readFile 不能读 .env 等敏感文件）。"""
    ReadFileTool.register_config(allowed_dirs=(str(tmp_path),))
    service = ToolService()
    service.register(ReadFileTool())

    result = await service.execute(
        "readFile", {"file_path": str(tmp_path.parent / "secret.env")}
    )

    assert result.success is False
    assert "不在允许目录内" in result.error
    assert result.error_code is None  # 业务错误，非系统级


@pytest.mark.asyncio
async def test_write_file_outside_allowed_dir_rejected(tmp_path):
    """白名单外路径被拒绝（writeFile 不能覆盖项目源码）。"""
    WriteFileTool.register_config(allowed_dirs=(str(tmp_path),))
    service = ToolService()
    service.register(WriteFileTool())

    result = await service.execute(
        "writeFile",
        {"file_path": str(tmp_path.parent / "settings.py"), "content": "x"},
    )

    assert result.success is False
    assert "不在允许目录内" in result.error


@pytest.mark.asyncio
async def test_file_path_traversal_rejected(tmp_path):
    """`..` 穿越逃出白名单被拒绝（abspath 规范化后比较）。"""
    ReadFileTool.register_config(allowed_dirs=(str(tmp_path),))
    service = ToolService()
    service.register(ReadFileTool())

    escape = tmp_path / ".." / "secret.env"
    result = await service.execute("readFile", {"file_path": str(escape)})

    assert result.success is False
    assert "不在允许目录内" in result.error


@pytest.mark.asyncio
async def test_allowed_dir_prefix_no_misjudge(tmp_path):
    """前缀目录不误放行：允许 <tmp>/data 时不放行 <tmp>/database（需 os.sep 分隔）。"""
    base = tmp_path / "data"
    ReadFileTool.register_config(allowed_dirs=(str(base),))
    service = ToolService()
    service.register(ReadFileTool())

    sibling = tmp_path / "database" / "secret.txt"
    result = await service.execute("readFile", {"file_path": str(sibling)})

    assert result.success is False
    assert "不在允许目录内" in result.error


@pytest.mark.asyncio
async def test_web_browse_on_unload_closes_client():
    """内置工具 on_unload 关闭全局 httpx 连接池（应用关闭回收，幂等）。"""
    tool = WebBrowseTool()
    web_browse._http_client = httpx.AsyncClient()

    await tool.on_unload()
    assert web_browse._http_client is None

    await tool.on_unload()  # 幂等：已关闭则跳过
    assert web_browse._http_client is None


class _FakeStream:
    """模拟子进程管道流：read 挂起（进程持续运行），直到被取消。"""

    async def read(self, n: int = -1) -> bytes:
        await asyncio.sleep(30)
        return b""


class _FakeSubprocessProc:
    """模拟长跑子进程：stdout/stderr 流读取挂起，等待外层超时取消。"""

    def __init__(self) -> None:
        self.killed = False
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_code_exec_timeout_kills_subprocess(monkeypatch):
    """executor 超时取消时，工具主动 kill 子进程，不留孤儿。"""
    captured: dict[str, _FakeSubprocessProc] = {}

    async def fake_create_subprocess_shell(*args, **kwargs) -> _FakeSubprocessProc:
        proc = _FakeSubprocessProc()
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(
        "app.integration.tools.builtin.code_exec.asyncio.create_subprocess_shell",
        fake_create_subprocess_shell,
    )

    service = ToolService()
    service.register(CodeExecTool())

    result = await service.execute("code_exec", {"command": "sleep 30"}, timeout=1)

    assert result.success is False
    assert result.error_code == ErrorCode.TIMEOUT
    assert "工具执行超时" in result.error
    # 关键：子进程已被 kill，executor 超时未导致孤儿进程残留
    assert captured["proc"].killed is True


@pytest.mark.asyncio
async def test_web_browse_rejects_ssrf_target():
    """内网裸 IP / 环回目标被 SSRF 防护拦截，不发起真实请求。"""
    service = ToolService()
    service.register(WebBrowseTool())

    result = await service.execute("web_browse", {"url": "http://127.0.0.1:8000/api"})

    assert result.success is False
    assert "SSRF" in result.error
    assert "裸 IP" in result.error


@pytest.mark.asyncio
async def test_read_file_large_chunked_head_tail(tmp_path):
    """超大文件分段读取（head+tail），限制内存占用，保留首尾。"""
    target = tmp_path / "huge.txt"
    head_pad = "A" * 50_000
    mid = "B" * 600_000
    tail_pad = "Z" * 50_000
    target.write_text(head_pad + mid + tail_pad, encoding="utf-8")

    tool = ReadFileTool()
    tool.register_config(allowed_dirs=(str(tmp_path),))

    result = await tool.execute(file_path=str(target))

    assert result.success is True
    assert "仅读取首尾" in result.content  # 工具层分段 marker
    assert "A" * 50_000 in result.content  # 头部保留
    assert "Z" * 50_000 in result.content  # 尾部保留
    assert ("B" * 600_000) not in result.content  # 中间被丢弃
    # 内存受限：分段读取量 ≈ 2×单段（head+tail），远小于完整文件 700k
    assert len(result.content) <= 620_000


class _FakeHugeStream:
    """模拟大输出管道流。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if not self._data:
            return b""
        chunk = self._data[:n]
        self._data = self._data[len(chunk) :]
        return chunk


class _FakeHugeProc:
    """模拟输出 40 万字节的子进程。"""

    def __init__(self) -> None:
        self.killed = False
        self.stdout = _FakeHugeStream(b"A" * 400_000)
        self.stderr = _FakeHugeStream(b"")

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
async def test_code_exec_output_capped(monkeypatch):
    """子进程超大输出被流式截断（保留前部），限制内存。"""
    captured: dict[str, _FakeHugeProc] = {}

    async def fake_create_subprocess_shell(*args, **kwargs) -> _FakeHugeProc:
        proc = _FakeHugeProc()
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(
        "app.integration.tools.builtin.code_exec.asyncio.create_subprocess_shell",
        fake_create_subprocess_shell,
    )

    service = ToolService()
    service.register(CodeExecTool())

    result = await service.execute("code_exec", {"command": "gen huge output"})

    assert result.success is True  # returncode 0
    # 输出被限制在 cap（max_output_length×3=300k）附近，而非完整 40 万
    assert len(result.content) <= 310_000
    assert result.content.startswith("A")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("良率正常".encode("utf-8"), "良率正常"),
        ("english only".encode("utf-8"), "english only"),
        (b"", ""),
    ],
)
def test_code_exec_decode_output_utf8(raw, expected):
    """UTF-8 合法输出按 UTF-8 解码（现代工具 / Python 脚本）。"""
    assert _decode_output(raw) == expected


@pytest.mark.skipif(
    locale.getpreferredencoding(False).lower() not in ("cp936", "gbk"),
    reason="非 GBK locale 环境不验证 Windows 中文输出回退",
)
def test_code_exec_decode_output_gbk_fallback():
    """GBK 编码输出（Windows cmd 系统命令）回退系统 locale 解码，不乱码。"""
    raw = "良率异常 98%→82%".encode("gbk")
    assert _decode_output(raw) == "良率异常 98%→82%"


class _FakeTavily:
    """模拟 Tavily 搜索响应。"""

    def __init__(self, response: dict) -> None:
        self._response = response

    def search(self, **kwargs) -> dict:
        return self._response


@pytest.mark.asyncio
async def test_search_answer_includes_source_urls(monkeypatch):
    """answer 路径来源 URL 进 metadata（证据链可回溯）。"""
    resp = {
        "answer": "良率骤降与 chamber 污染相关",
        "results": [
            {"title": "A", "content": "内容A", "url": "https://a.com"},
            {"title": "B", "content": "内容B", "url": "https://b.com"},
        ],
    }
    monkeypatch.setattr(
        "app.integration.tools.builtin.search.TavilyClient",
        lambda api_key: _FakeTavily(resp),
    )
    tool = SearchTool()
    tool.register_config(api_key="test-key")

    result = await tool.execute(query="良率 异常")

    assert result.success is True
    assert result.content == resp["answer"]
    assert result.metadata["source"] == "tavily_answer"
    assert result.metadata["urls"] == ["https://a.com", "https://b.com"]


@pytest.mark.asyncio
async def test_search_results_include_source_urls(monkeypatch):
    """搜索结果行尾追加来源 URL（证据可回溯）。"""
    resp = {
        "results": [
            {"title": "A", "content": "内容A", "url": "https://a.com"},
            {"title": "B", "content": "内容B", "url": "https://b.com"},
        ],
    }
    monkeypatch.setattr(
        "app.integration.tools.builtin.search.TavilyClient",
        lambda api_key: _FakeTavily(resp),
    )
    tool = SearchTool()
    tool.register_config(api_key="test-key")

    result = await tool.execute(query="测试")

    assert result.success is True
    assert "来源: https://a.com" in result.content
    assert "来源: https://b.com" in result.content


@pytest.mark.asyncio
async def test_search_result_missing_url_tolerated(monkeypatch):
    """Tavily 结果缺 url 字段时不崩溃（.get 兜底）。"""
    resp = {"results": [{"title": "A", "content": "内容A"}]}
    monkeypatch.setattr(
        "app.integration.tools.builtin.search.TavilyClient",
        lambda api_key: _FakeTavily(resp),
    )
    tool = SearchTool()
    tool.register_config(api_key="test-key")

    result = await tool.execute(query="测试")

    assert result.success is True
    assert "内容A" in result.content
