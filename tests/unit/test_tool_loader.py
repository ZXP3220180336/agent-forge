"""
ExternalToolLoader 单元测试

覆盖：
    加载（首扫 / 新增文件）
    重载（mtime 变化 → 卸载旧实例 + 加载新实例）
    卸载（删除文件 → 注销）
    同名冲突拒绝（builtin 权威）
    语法错误 / 目录不存在 / 无工具类 → 跳过不崩溃
    文件级回滚（单文件多工具部分失败 → 全回滚）
    生命周期钩子 on_load / on_unload / health_check
    execute 惰性检查（maybe_refresh：签名不变零重扫 / 变化触发重扫）
    排除 __init__.py / _ 开头；非法文件名（my-tool.py）走哈希模块名
"""

import os
from pathlib import Path

import pytest

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.base import BaseTool
from app.integration.tools.loader import ExternalToolLoader
from app.integration.tools.tool_service import ToolService


def _tool_source(name: str, content: str = "ok", *, extra: str = "") -> str:
    """生成一个继承 BaseTool 的工具文件源码（类名 = name.title() + Tool）。"""
    return f'''\
from app.integration.tools.base import BaseTool
from app.domain.ports.tool_gateway import ToolResult

class {name.title()}Tool(BaseTool):
    @property
    def name(self): return "{name}"
    @property
    def description(self): return "{name} tool"
    @property
    def parameters(self): return {{"type": "object", "properties": {{}}, "required": []}}
    async def execute(self, **kwargs): return ToolResult(success=True, content="{content}")
{extra}
'''


def _write_tool_file(tmp_path: Path, filename: str, source: str) -> Path:
    p = tmp_path / filename
    p.write_text(source, encoding="utf-8")
    return p


class _FixedTool(BaseTool):
    """预注册工具（模拟 builtin 权威，用于冲突测试）。"""

    name = "dup"
    _content = "original"

    @property
    def description(self) -> str:
        return "fixed tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content=self._content)


@pytest.fixture
def service() -> ToolService:
    return ToolService()


@pytest.fixture
def loader(service: ToolService, tmp_path: Path) -> ExternalToolLoader:
    return ExternalToolLoader(service, default_directory=str(tmp_path))


@pytest.mark.asyncio
async def test_load_new_tool(service, loader, tmp_path):
    """首扫：写入工具文件 → scan → 工具注册且可 execute。"""
    _write_tool_file(tmp_path, "echo_tool.py", _tool_source("echo"))
    await loader.scan_once()

    assert service.get("echo") is not None
    result = await service.execute("echo", {})
    assert result.success is True
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_add_file_on_later_scan(service, loader, tmp_path):
    """已有目录再新增文件 → 下次 scan 注册。"""
    _write_tool_file(tmp_path, "a.py", _tool_source("alpha"))
    await loader.scan_once()
    assert service.get("alpha") is not None

    _write_tool_file(tmp_path, "b.py", _tool_source("beta"))
    await loader.scan_once()
    assert service.get("beta") is not None


@pytest.mark.asyncio
async def test_reload_on_mtime_change(service, loader, tmp_path):
    """修改文件（mtime 变化）→ 重载：新实例注册、旧实例替换。"""
    p = _write_tool_file(tmp_path, "ver.py", _tool_source("ver", content="v1"))
    await loader.scan_once()
    assert (await service.execute("ver", {})).content == "v1"

    p.write_text(_tool_source("ver", content="v2"), encoding="utf-8")
    # 显式推进 mtime（不依赖文件系统自然时间粒度）
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 2))
    await loader.scan_once()

    assert (await service.execute("ver", {})).content == "v2"


@pytest.mark.asyncio
async def test_delete_file_unloads(service, loader, tmp_path):
    """删除文件 → 下次 scan 注销。"""
    p = _write_tool_file(tmp_path, "gone.py", _tool_source("gone"))
    await loader.scan_once()
    assert service.get("gone") is not None

    p.unlink()
    await loader.scan_once()
    assert service.get("gone") is None


@pytest.mark.asyncio
async def test_conflict_with_registered_skipped(service, loader, tmp_path):
    """与已注册工具同名 → 跳过且不覆盖（builtin 权威）。"""
    service.register(_FixedTool())
    _write_tool_file(tmp_path, "dup.py", _tool_source("dup", content="dup_content"))
    await loader.scan_once()

    tool = service.get("dup")
    assert isinstance(tool, _FixedTool)  # 仍是预注册实例，未被替换
    assert (await service.execute("dup", {})).content == "original"


@pytest.mark.asyncio
async def test_syntax_error_file_skipped(service, loader, tmp_path):
    """语法错误文件 → 跳过不崩溃，其余文件照常。"""
    _write_tool_file(tmp_path, "bad.py", "def :\n")
    _write_tool_file(tmp_path, "good.py", _tool_source("good"))
    await loader.scan_once()

    assert service.get("bad") is None
    assert service.get("good") is not None


@pytest.mark.asyncio
async def test_directory_missing_noop(service, tmp_path):
    """目录不存在 → scan 不抛异常。"""
    loader = ExternalToolLoader(service, default_directory=str(tmp_path / "nope"))
    await loader.scan_once()


@pytest.mark.asyncio
async def test_file_rollback_on_partial_failure(service, loader, tmp_path):
    """单文件多工具，第二个实例化抛异常 → 第一个也被回滚（文件级原子性）。"""
    source = '''\
from app.integration.tools.base import BaseTool
from app.domain.ports.tool_gateway import ToolResult

class GoodTool(BaseTool):
    @property
    def name(self): return "good"
    @property
    def description(self): return "good tool"
    @property
    def parameters(self): return {"type": "object", "properties": {}, "required": []}
    async def execute(self, **kwargs): return ToolResult(success=True, content="ok")

class BadTool(BaseTool):
    def __init__(self): raise RuntimeError("init boom")
    @property
    def name(self): return "bad"
    @property
    def description(self): return "bad tool"
    @property
    def parameters(self): return {"type": "object", "properties": {}, "required": []}
    async def execute(self, **kwargs): return ToolResult(success=True, content="ok")
'''
    _write_tool_file(tmp_path, "multi.py", source)
    await loader.scan_once()

    assert service.get("good") is None  # 被回滚
    assert service.get("bad") is None


@pytest.mark.asyncio
async def test_on_load_called(service, loader, tmp_path):
    """加载时调用 on_load。"""
    source = _tool_source(
        "spy",
        extra='''\
    loaded = False
    async def on_load(self):
        SpyTool.loaded = True
''',
    )
    _write_tool_file(tmp_path, "spy.py", source)
    await loader.scan_once()

    assert service.get("spy").loaded is True


@pytest.mark.asyncio
async def test_on_load_failure_skips_tool(service, loader, tmp_path):
    """on_load 抛异常 → 该工具跳过 + 回滚。"""
    source = _tool_source(
        "fload",
        extra='''\
    async def on_load(self):
        raise RuntimeError("load fail")
''',
    )
    _write_tool_file(tmp_path, "fload.py", source)
    await loader.scan_once()

    assert service.get("fload") is None


@pytest.mark.asyncio
async def test_on_unload_called(service, loader, tmp_path):
    """卸载时调用 on_unload 后再注销。"""
    source = _tool_source(
        "ud",
        extra='''\
    unloaded = False
    async def on_unload(self):
        UdTool.unloaded = True
''',
    )
    p = _write_tool_file(tmp_path, "ud.py", source)
    await loader.scan_once()
    tool = service.get("ud")

    p.unlink()
    await loader.scan_once()

    assert tool.unloaded is True
    assert service.get("ud") is None


@pytest.mark.asyncio
async def test_health_check_default_true(service, loader, tmp_path):
    """health_check 默认返回 True（接口预留）。"""
    _write_tool_file(tmp_path, "hc.py", _tool_source("hc"))
    await loader.scan_once()

    assert await service.get("hc").health_check() is True


@pytest.mark.asyncio
async def test_maybe_refresh_skips_when_unchanged(service, loader, tmp_path):
    """目录无变化 → maybe_refresh 不重扫（工具保留）。"""
    _write_tool_file(tmp_path, "a.py", _tool_source("alpha"))
    await loader.scan_once()
    assert service.get("alpha") is not None

    await loader.maybe_refresh()
    assert service.get("alpha") is not None


@pytest.mark.asyncio
async def test_maybe_refresh_detects_new_file(service, loader, tmp_path):
    """目录新增文件 → maybe_refresh（execute 入口语义）触发注册。"""
    _write_tool_file(tmp_path, "a.py", _tool_source("alpha"))
    await loader.scan_once()

    _write_tool_file(tmp_path, "b.py", _tool_source("beta"))
    await loader.maybe_refresh()
    assert service.get("beta") is not None


@pytest.mark.asyncio
async def test_init_and_private_files_excluded(service, loader, tmp_path):
    """__init__.py 与 _ 开头文件被排除。"""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    _write_tool_file(tmp_path, "_private.py", _tool_source("priv"))
    await loader.scan_once()

    assert service.get("priv") is None


@pytest.mark.asyncio
async def test_hyphen_filename_loaded(service, loader, tmp_path):
    """非法标识符文件名（my-tool.py）→ 哈希模块名加载成功。"""
    _write_tool_file(tmp_path, "my-tool.py", _tool_source("mytool"))
    await loader.scan_once()

    assert service.get("mytool") is not None
    assert (await service.execute("mytool", {})).content == "ok"


# 声明 CONFIG_KEYS + register_config 的外部工具源码（配置注入契约演示）
_CONFIG_TOOL_SOURCE = '''\
from app.integration.tools.base import BaseTool
from app.domain.ports.tool_gateway import ToolResult

CONFIG_KEYS = ("custom_timeout",)

class CfgTool(BaseTool):
    _timeout = 5.0
    @classmethod
    def register_config(cls, *, custom_timeout=None, **kw):
        if custom_timeout is not None:
            cls._timeout = custom_timeout
    @property
    def name(self): return "cfg_tool"
    @property
    def description(self): return "config tool"
    @property
    def parameters(self): return {"type": "object", "properties": {}, "required": []}
    async def execute(self, **kwargs): return ToolResult(success=True, content="ok")
'''


@pytest.mark.asyncio
async def test_load_without_config_source_keeps_default(service, loader, tmp_path):
    """无 config_source 时跳过注入（工具保持模块默认配置，不报错）。"""
    _write_tool_file(tmp_path, "cfg_tool.py", _CONFIG_TOOL_SOURCE)
    await loader.scan_once()

    tool = service.get("cfg_tool")
    assert tool is not None
    assert tool._timeout == 5.0  # 默认值，未被注入


@pytest.mark.asyncio
async def test_load_injects_config_from_source(service, tmp_path):
    """模块声明 CONFIG_KEYS 时，loader 经 config_source 注入 register_config（对齐内置工具风格）。"""
    _write_tool_file(tmp_path, "cfg_tool.py", _CONFIG_TOOL_SOURCE)
    loader = ExternalToolLoader(
        service,
        default_directory=str(tmp_path),
        config_source=lambda k: 25.0 if k == "custom_timeout" else None,
    )
    await loader.scan_once()

    tool = service.get("cfg_tool")
    assert tool is not None
    assert tool._timeout == 25.0  # register_config 已收到注入值


@pytest.mark.asyncio
async def test_maybe_refresh_ttl_skips_within_interval(
    service, loader, tmp_path, monkeypatch
):
    """TTL 内连续 maybe_refresh 短路，不重复磁盘 stat（热路径零 IO）。"""
    _write_tool_file(tmp_path, "a.py", _tool_source("alpha"))
    await loader.scan_once()

    stat_calls = {"n": 0}
    orig_signature = ExternalToolLoader._dir_signature

    def spy(self):
        stat_calls["n"] += 1
        return orig_signature(self)

    monkeypatch.setattr(ExternalToolLoader, "_dir_signature", spy)

    await loader.maybe_refresh()  # 首次：检查（stat 1 次）
    await loader.maybe_refresh()  # TTL 内二次：短路，不 stat

    assert stat_calls["n"] == 1


@pytest.mark.asyncio
async def test_maybe_refresh_expired_ttl_rechecks(
    service, loader, tmp_path, monkeypatch
):
    """TTL 到期后 maybe_refresh 重新检查（变更最多延迟 1s 生效）。"""
    _write_tool_file(tmp_path, "a.py", _tool_source("alpha"))
    await loader.scan_once()

    _write_tool_file(tmp_path, "b.py", _tool_source("beta"))
    loader._last_dir_check = 0.0  # 强制 TTL 过期（模拟 ≥1s 后再次调用）

    await loader.maybe_refresh()
    assert service.get("beta") is not None
