"""
内置工具真实执行集成测试

用真实 ToolService + 内置工具验证端到端执行（不依赖网络）：
- writeFile / readFile：tmp_path 真实读写，含父目录自动创建与缺失文件错误
- code_exec：真实子进程执行安全命令 + 危险命令前缀拦截
- search：未配置 key 时的优雅失败（显式重置，避免读到 .env 真实 key 触发网络）
- init_default_tools 装配完整性（5 个内置工具）
"""

import asyncio
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


class _FakeSubprocessProc:
    """模拟长跑子进程：首次 communicate 挂起（被取消），kill 后清理立即返回。"""

    def __init__(self) -> None:
        self.killed = False
        self._communicate_count = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self._communicate_count += 1
        if self._communicate_count == 1:
            await asyncio.sleep(30)  # 首次：模拟长时间运行，等待外层超时取消
        return b"", b""  # 二次（kill 后清理管道）：立即返回

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
