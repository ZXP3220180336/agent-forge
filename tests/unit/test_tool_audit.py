"""
ToolAuditor 安全审计单元测试

覆盖：
    RiskLevel 分级值/顺序
    record → 结构化事件日志（event_name="tool_call"）字段完整性
    L2 起 WARNING、L0/L1 INFO 的级别映射
    enabled=False 静默关闭
    params 超长截断（防 writeFile 整内容进日志）
"""

import logging

import pytest

from app.integration.tools.security import RiskLevel, ToolAuditor


def test_risk_level_values_and_order():
    """L0 只读 / L1 写 / L2 危险 / L3 禁用，可排序比较。"""
    assert RiskLevel.L0_READONLY.value == 0
    assert RiskLevel.L1_WRITE.value == 1
    assert RiskLevel.L2_DANGEROUS.value == 2
    assert RiskLevel.L3_DISABLED.value == 3
    assert RiskLevel.L2_DANGEROUS > RiskLevel.L1_WRITE > RiskLevel.L0_READONLY


@pytest.mark.asyncio
async def test_audit_records_structured_fields(caplog):
    """record → tool_call 事件，字段完整（tool/risk_level/category/success/elapsed/params）。"""
    auditor = ToolAuditor()
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="readFile",
            risk_level=RiskLevel.L0_READONLY,
            category="file",
            success=True,
            elapsed=0.5,
            parameters={"file_path": "/tmp/a.txt"},
        )

    assert len(caplog.records) == 1
    rec = caplog.records[0]
    assert rec.message == "tool_call"
    assert rec.tool == "readFile"
    assert rec.risk_level == "L0_READONLY"
    assert rec.category == "file"
    assert rec.success is True
    assert rec.elapsed == 0.5
    assert rec.params == '{"file_path": "/tmp/a.txt"}'


@pytest.mark.asyncio
async def test_audit_l2_uses_warning_level(caplog):
    """L2 危险工具 → WARNING（便于 ops 检索）。"""
    auditor = ToolAuditor()
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="code_exec",
            risk_level=RiskLevel.L2_DANGEROUS,
            category="code",
            success=True,
            elapsed=1.0,
            parameters={},
        )

    assert caplog.records[-1].levelname == "WARNING"


@pytest.mark.asyncio
async def test_audit_l0_uses_info_level(caplog):
    """L0 只读工具 → INFO。"""
    auditor = ToolAuditor()
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="search",
            risk_level=RiskLevel.L0_READONLY,
            category="search",
            success=False,
            elapsed=0.1,
            parameters={},
            error="未配置 key",
        )

    assert caplog.records[-1].levelname == "INFO"
    assert caplog.records[-1].error == "未配置 key"


@pytest.mark.asyncio
async def test_audit_disabled_silent(caplog):
    """enabled=False 时无任何输出。"""
    auditor = ToolAuditor(enabled=False)
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="x",
            risk_level=RiskLevel.L0_READONLY,
            category="general",
            success=True,
            elapsed=0.1,
            parameters={},
        )

    assert caplog.records == []


@pytest.mark.asyncio
async def test_audit_params_truncated(caplog):
    """params 超长截断（防 writeFile 把整文件内容写进日志）。"""
    auditor = ToolAuditor(params_max_chars=10)
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="writeFile",
            risk_level=RiskLevel.L1_WRITE,
            category="file",
            success=True,
            elapsed=0.1,
            parameters={"content": "A" * 100},
        )

    assert len(caplog.records[-1].params) <= 10
