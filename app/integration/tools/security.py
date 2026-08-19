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

import asyncio
import ipaddress
import json
import logging
import socket
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Protocol

from app.domain.ports.tool_gateway import ErrorCode
from app.platform.observability.logger import log_event_async

if TYPE_CHECKING:
    import httpx


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
        error_code: ErrorCode | None = None,
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
            error_code=error_code.name if error_code else None,
            content=content_preview[: self._content_preview_chars],
        )


class ApprovalGate(Protocol):
    """审批通道协议：执行前征得人工 / 策略确认。"""

    async def request(self, tool_name: str, parameters: dict[str, Any]) -> bool: ...


class AutoApprovalGate:
    """默认审批通道：一律放行（当前行为，不拦截）。"""

    async def request(self, tool_name: str, parameters: dict[str, Any]) -> bool:
        return True


# ===== SSRF 防护：拒绝内网 / 环回 / 保留网段与内网保留域名（web_browse / http_api 共享） =====

# 内网 / 保留 TLD 后缀（DNS 私有域，解析结果不可信或指向内网）
_PRIVATE_TLD_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".localhost",
    ".internal",
    ".corp",
    ".home",
    ".lan",
    ".intranet",
    ".private",
    ".test",
    ".example",
)


class SSRFError(Exception):
    """目标 URL 命中 SSRF 防护规则。"""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """命中内网 / 环回 / 链路本地 / 保留 / 未指定 / 组播段（IPv4-mapped IPv6 先归一）。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def check_host_sync(hostname: str) -> None:
    """校验主机名：裸 IP / 内网保留域名 / 解析后命中内网站段 → 抛 SSRFError。"""
    # 1. 裸 IP：一律拒绝（保守策略，仅允许域名访问）
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None:
        raise SSRFError(f"目标为裸 IP，拒绝访问: {hostname}")

    # 2. 内网保留域名后缀
    lowered = hostname.rstrip(".").lower()
    if any(lowered.endswith(suffix) for suffix in _PRIVATE_TLD_SUFFIXES):
        raise SSRFError(f"目标域名命中内网保留后缀，拒绝访问: {hostname}")

    # 3. 域名解析：任一 IP 命中拦截段即拒绝（防 DNS rebinding）
    try:
        infos = socket.getaddrinfo(lowered, None)
    except socket.gaierror as e:
        raise SSRFError(f"目标域名解析失败: {hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise SSRFError(f"目标域名 {hostname} 解析到内网/环回地址: {ip}")


async def check_host_async(hostname: str) -> None:
    """异步版：DNS 解析经 asyncio.to_thread，不阻塞事件循环。"""
    await asyncio.to_thread(check_host_sync, hostname)


async def ssrf_on_request(request: httpx.Request) -> None:
    """每个请求（含重定向跳）前校验目标 host，命中防护规则即中断。"""
    await check_host_async(request.url.host)
