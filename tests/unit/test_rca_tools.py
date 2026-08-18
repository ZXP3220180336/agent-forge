"""
良率 RCA 工具单元测试

覆盖：
    5 个 RCA 工具正常查询 + 证据链 metadata（source / 查询键）
    参数校验失败 / 业务未找到
    LOT-A123 根因故事：良率骤降 → FDC 偏离 → center_cluster 缺陷 → 历史案例
    模拟数据可复现（固定数据）
    init_default_tools 装配 10 个内置工具（5 通用 + 5 RCA）
"""

import pytest

from app.integration.tools.builtin.rca.alerts_tool import QueryEquipmentAlertsTool
from app.integration.tools.builtin.rca.data import query_fdc, query_yield
from app.integration.tools.builtin.rca.defect_tool import QueryDefectMapTool
from app.integration.tools.builtin.rca.fdc_tool import QueryFdcParamsTool
from app.integration.tools.builtin.rca.history_tool import SearchHistoricalRcaTool
from app.integration.tools.builtin.rca.yield_tool import QueryBatchYieldTool
from app.integration.tools.tool_service import ToolService


@pytest.mark.asyncio
async def test_query_batch_yield_lot_a123_drop():
    """LOT-A123 良率骤降（82%）→ 识别标记 + 证据链 metadata。"""
    result = await QueryBatchYieldTool().execute(batch_id="LOT-A123")

    assert result.success is True
    assert "82.0" in result.content
    assert "骤降" in result.content
    assert result.metadata["source"] == "mock_yms"
    assert result.metadata["batch_id"] == "LOT-A123"


@pytest.mark.asyncio
async def test_query_batch_yield_not_found():
    """批次不存在 → 业务失败 + 中文归因。"""
    result = await QueryBatchYieldTool().execute(batch_id="NOPE")

    assert result.success is False
    assert "未找到" in result.error


@pytest.mark.asyncio
async def test_query_alerts_filters_by_equipment():
    """ETCH-01 告警：chamber pressure ALARM + PM 记录。"""
    result = await QueryEquipmentAlertsTool().execute(equipment_id="ETCH-01")

    assert result.success is True
    assert "chamber pressure" in result.content
    assert result.metadata["source"] == "mock_mes"


@pytest.mark.asyncio
async def test_query_fdc_detects_pressure_deviation():
    """ETCH-01 FDC：chamber_pressure 偏离 +12%（根因关键证据）。"""
    result = await QueryFdcParamsTool().execute(equipment_id="ETCH-01")

    assert result.success is True
    assert "chamber_pressure" in result.content
    assert "12.0%" in result.content
    assert "偏离" in result.content


@pytest.mark.asyncio
async def test_query_defect_center_cluster_particle():
    """LOT-A123 缺陷：center_cluster 模式 + particle 主导类型。"""
    result = await QueryDefectMapTool().execute(batch_id="LOT-A123")

    assert result.success is True
    assert "center_cluster" in result.content
    assert "particle" in result.content
    assert result.metadata["batch_id"] == "LOT-A123"


@pytest.mark.asyncio
async def test_search_history_hits_relevant_case():
    """关键词检索命中与 LOT-A123 根因匹配的历史案例（RCA-001）。"""
    result = await SearchHistoricalRcaTool().execute(
        query="etch 偏离 良率 骤降"
    )

    assert result.success is True
    assert "RCA-001" in result.content
    assert "chamber" in result.content
    assert result.metadata["top_k"] == 3


@pytest.mark.asyncio
async def test_validation_failure_missing_required():
    """缺必填参数 → 校验兜底失败。"""
    result = await QueryBatchYieldTool().execute()

    assert result.success is False
    assert "参数有误" in result.error


@pytest.mark.asyncio
async def test_business_not_found_per_tool():
    """各工具业务未找到路径（机台 / 批次 / 案例）。"""
    assert (await QueryFdcParamsTool().execute(equipment_id="GHOST")).success is False
    assert (await QueryDefectMapTool().execute(batch_id="GHOST")).success is False
    assert (
        (await SearchHistoricalRcaTool().execute(query="zzz 无匹配 关键词")).success
    ) is False


@pytest.mark.asyncio
async def test_mock_data_reproducible():
    """固定数据可复现（两次查询结果一致，无随机性）。"""
    assert query_yield("LOT-A123") == query_yield("LOT-A123")
    assert query_fdc("ETCH-01") == query_fdc("ETCH-01")


@pytest.mark.asyncio
async def test_init_default_tools_registers_all():
    """装配完整性：10 个内置工具（5 通用 + 5 RCA）。"""
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
