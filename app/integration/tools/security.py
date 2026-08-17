"""工具安全与审计：风险分级 + 审计留痕 + 人工审批通道。

设计决策（见 ADR `adr/integration/tools/2026-08-17-risk-levels-audit-no-enforcement.md`）：
- 风险分级 L0-L3 是工业界共识（L0 只读 / L1 写 / L2 危险 / L3 禁用）
- 审计只做「分级标注 + 留痕」；审计独立于 ExecutionHooks：hooks 是可扩展的
  通知机制（仅成功路径），审计是系统级强制留痕，须覆盖成功 / 失败 / 未注册 /
  校验失败 / 超时全部路径
- 审批由 ApprovalGate 在 executor 执行前执行：`BaseTool.requires_approval` 是
  元数据声明，默认 `AutoApprovalGate` 放行（保持不拦截），未来接真实审批
  （API 确认 / 管理端审批 / 策略引擎）仅换注入实现，Agent 层与 ToolGateway 零改动
"""

from __future__ import annotations

import json
import logging
from enum import IntEnum
from typing import Any, Protocol

from app.platform.observability.logger import log_event_async


class RiskLevel(IntEnum):
    """工具风险分级（仅元数据标注 + 审计，不做执行拦截）。"""

    L0_READONLY = 0  # 只读：search / readFile / web_browse
    L1_WRITE = 1  # 写操作：writeFile
    L2_DANGEROUS = 2  # 危险：code_exec
    L3_DISABLED = 3  # 禁用（预留：风险过高禁止注册）


class ToolAuditor:
    """审计留痕：记录到结构化日志（event_name="tool_call"）。不拦截执行。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        params_max_chars: int = 1_000,
        content_preview_chars: int = 200,
    ) -> None:
        self._enabled = enabled
        self._params_max_chars = params_max_chars
        self._content_preview_chars = content_preview_chars

    async def record(
        self,
        *,
        tool_name: str,
        risk_level: RiskLevel,
        category: str,
        success: bool,
        elapsed: float,
        parameters: dict[str, Any],
        error: str | None = None,
        retry_count: int = 0,
        content_preview: str = "",
    ) -> None:
        """记录一次工具调用审计（每次 execute 一条最终结果，不做 per-attempt）。"""
        if not self._enabled:
            return

        # 日志级别映射：L0/L1→INFO；L2→WARNING；L3→ERROR（便于 ops 按级别检索）
        if risk_level >= RiskLevel.L3_DISABLED:
            level = logging.ERROR
        elif risk_level >= RiskLevel.L2_DANGEROUS:
            level = logging.WARNING
        else:
            level = logging.INFO

        params_json = json.dumps(parameters, ensure_ascii=False, default=str)
        await log_event_async(
            "tool_call",
            level=level,
            tool=tool_name,
            risk_level=risk_level.name,
            category=category,
            success=success,
            elapsed=round(elapsed, 4),
            retry_count=retry_count,
            params=params_json[: self._params_max_chars],
            error=error,
            content=content_preview[: self._content_preview_chars],
        )


class ApprovalGate(Protocol):
    """审批通道协议：执行前征得人工 / 策略确认。"""

    async def request(self, tool_name: str, parameters: dict[str, Any]) -> bool: ...


class AutoApprovalGate:
    """默认审批通道：一律放行（当前行为，不拦截）。"""

    async def request(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        return True
