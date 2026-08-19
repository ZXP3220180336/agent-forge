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

from app.domain.ports.tool_gateway import ErrorCode
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


@pytest.mark.asyncio
async def test_audit_records_error_code(caplog):
    """error_code 记录到 tool_call 事件（失败可机器分类，供证据链聚合）。"""
    auditor = ToolAuditor()
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="readFile",
            risk_level=RiskLevel.L0_READONLY,
            category="file",
            success=False,
            elapsed=0.1,
            parameters={},
            error="文件不存在",
            error_code=ErrorCode.TIMEOUT,
        )

    assert caplog.records[-1].error_code == "TIMEOUT"


@pytest.mark.asyncio
async def test_audit_error_code_defaults_none(caplog):
    """未传 error_code（成功 / 业务错误）→ 日志字段为 None。"""
    auditor = ToolAuditor()
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="search",
            risk_level=RiskLevel.L0_READONLY,
            category="search",
            success=True,
            elapsed=0.1,
            parameters={},
        )

    assert caplog.records[-1].error_code is None


def test_audit_masks_sensitive_keys():
    """审计参数敏感键（api_key/token/password/authorization）掩码，防凭据落盘。"""
    auditor = ToolAuditor()
    masked = auditor._mask_sensitive(
        {
            "query": "正常",
            "api_key": "sk-secret",
            "headers": {"Authorization": "Bearer tok"},
            "nested": {"password": "pw", "ok": 1},
            "monkey": "不掩码",  # key 词边界不误伤
        }
    )
    assert masked["api_key"] == "***"
    assert masked["headers"]["Authorization"] == "***"
    assert masked["nested"]["password"] == "***"
    assert masked["monkey"] == "不掩码"
    assert masked["query"] == "正常"
    assert masked["nested"]["ok"] == 1


@pytest.mark.asyncio
async def test_audit_redacts_sensitive_in_log(caplog):
    """record 落日志时敏感键值被掩码（防凭据泄露到审计日志）。"""
    auditor = ToolAuditor()
    with caplog.at_level(logging.INFO, logger="app.events"):
        await auditor.record(
            tool_name="http_api",
            risk_level=RiskLevel.L1_WRITE,
            category="http",
            success=False,
            elapsed=0.5,
            parameters={
                "url": "https://api.x",
                "headers": {"Authorization": "Bearer tok"},
            },
        )

    rec = caplog.records[0]
    assert "Bearer tok" not in rec.params  # 明文凭据不落盘
    assert "***" in rec.params
