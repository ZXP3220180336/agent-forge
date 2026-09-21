"""只读适配器通过受控入口执行，保持业务结果与证据字段。"""

import asyncio
import os
from unittest.mock import Mock

import pytest

from app.integration.tools.builtin.file_ops import ReadFileTool, WriteFileTool
from app.integration.tools.builtin.rca.alerts_tool import QueryEquipmentAlertsTool
from app.integration.tools.builtin.rca.defect_tool import QueryDefectMapTool
from app.integration.tools.builtin.rca.fdc_tool import QueryFdcParamsTool
from app.integration.tools.builtin.rca.history_tool import SearchHistoricalRcaTool
from app.integration.tools.builtin.rca.yield_tool import QueryBatchYieldTool
from app.integration.tools.builtin.search import SearchTool
from app.integration.tools.builtin.web_browse import WebBrowseTool
from app.integration.tools.execution import ToolEffectClass


class RecordingExecution:
    """验证适配器委托边界；真实线程所有权由 Supervisor 测试覆盖。"""

    def __init__(self):
        self.calls = 0
        self.checked = False

    def check_abort(self):
        self.checked = True

    async def run_sync(self, fn, *args, **kwargs):
        self.calls += 1
        return await asyncio.to_thread(fn, *args, **kwargs)


@pytest.mark.parametrize(
    "tool_type",
    [
        SearchTool,
        ReadFileTool,
        WebBrowseTool,
        QueryEquipmentAlertsTool,
        QueryDefectMapTool,
        QueryFdcParamsTool,
        SearchHistoricalRcaTool,
        QueryBatchYieldTool,
    ],
)
def test_readonly_spec_is_explicit(tool_type):
    assert tool_type().describe_execution({}).effect_class == ToolEffectClass.READ_ONLY


def test_write_tool_explicitly_declares_write_effect():
    assert WriteFileTool().describe_execution({}).effect_class == ToolEffectClass.MAY_WRITE


async def test_search_uses_tracked_worker_and_explicit_sdk_timeout(monkeypatch):
    client = Mock()
    client.search.return_value = {
        "answer": "结果",
        "results": [{"url": "https://example.com/source"}],
    }
    factory = Mock(return_value=client)
    monkeypatch.setattr("app.integration.tools.builtin.search.TavilyClient", factory)
    tool = SearchTool()
    monkeypatch.setattr(tool, "_api_key", "test-key")
    execution = RecordingExecution()
    result = await tool.invoke({"query": "问题"}, execution)
    assert execution.calls == 1
    factory.assert_called_once_with(api_key="test-key", timeout=15)
    client.search.assert_called_once_with(query="问题", search_depth="basic", include_answer=True)
    assert result.success
    assert result.metadata["urls"] == ["https://example.com/source"]
    assert not tool.can_retry(result)


async def test_read_file_worker_closes_file_and_preserves_head_tail(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    path.write_text("abcdefghij" * 10, encoding="utf-8")
    tool = ReadFileTool()
    monkeypatch.setattr(tool, "_allowed_dirs", (os.path.normcase(str(tmp_path)),))
    monkeypatch.setattr(tool, "_max_output_length", 2)
    execution = RecordingExecution()
    result = await tool.invoke({"file_path": str(path)}, execution)
    assert execution.calls == 1
    assert result.success
    assert result.content.startswith("abcdef")
    assert result.content.endswith("efghij")
    assert "仅读取首尾" in result.content
    path.unlink()  # Windows 上验证同步工作已关闭文件。


async def test_default_invoke_preserves_rca_evidence():
    execution = RecordingExecution()
    result = await QueryBatchYieldTool().invoke({"batch_id": "LOT-A123"}, execution)
    assert execution.checked
    assert execution.calls == 0
    assert result.success
    assert result.metadata["source"] == "mock_yms"
    assert result.metadata["batch_id"] == "LOT-A123"
    assert result.metadata["timestamp"]
