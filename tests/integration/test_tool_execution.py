"""
内置工具真实执行集成测试

用真实 ToolService + 内置工具验证端到端执行（不依赖网络）：
- writeFile / readFile：tmp_path 真实读写，含父目录自动创建与缺失文件错误
- code_exec：真实子进程执行安全命令 + 危险命令前缀拦截
- search：未配置 key 时的优雅失败（显式重置，避免读到 .env 真实 key 触发网络）
- init_default_tools 装配完整性（5 个内置工具）
"""

import sys

import pytest

from app.integration.tools.builtin import (
    CodeExecTool,
    ReadFileTool,
    SearchTool,
    WriteFileTool,
)
from app.integration.tools.tool_service import ToolService


@pytest.mark.asyncio
async def test_write_and_read_file_roundtrip(tmp_path):
    """writeFile → readFile 真实读写，父目录自动创建"""
    target = tmp_path / "nested" / "out.txt"
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
async def test_init_default_tools_registers_five():
    """装配根一键注册 5 个内置工具"""
    service = ToolService()
    registered = service.init_default_tools()

    assert len(registered) == 5
    assert set(service.list_tools()) == {
        "search",
        "readFile",
        "writeFile",
        "code_exec",
        "web_browse",
    }


@pytest.mark.asyncio
async def test_read_file_large_content_truncated_head_tail(tmp_path):
    """readFile 大文件结果由 ResultProcessor 统一 head+tail 截断（含 marker）。"""
    target = tmp_path / "big.txt"
    # 超过 readFile 默认 max_output_length（100_000）
    content = ("头" * 500) + ("中" * 100_000) + ("尾" * 500)
    target.write_text(content, encoding="utf-8")

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
