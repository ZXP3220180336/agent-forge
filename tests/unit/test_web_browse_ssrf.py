"""
web_browse SSRF 防护单元测试
"""

import socket

import pytest

from app.integration.tools.security import SSRFError, check_host_sync


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # 环回
        "10.0.0.1",  # 私网 A
        "192.168.1.1",  # 私网 C
        "172.16.0.1",  # 私网 B
        "169.254.169.254",  # 云元数据（链路本地）
        "0.0.0.0",  # 未指定
        "::1",  # IPv6 环回
        "8.8.8.8",  # 公网裸 IP（保守策略同样拒绝）
    ],
)
def test_ssrf_blocks_bare_ips(host):
    """裸 IP（含公网）一律拒绝。"""
    with pytest.raises(SSRFError):
        check_host_sync(host)


@pytest.mark.parametrize(
    "host",
    [
        "host.internal",
        "printer.local",
        "x.localhost",
        "svc.corp",
        "server.home",
        "nas.lan",
        "portal.private",
        "api.test",
        "dev.example",
        "HOST.INTERNAL",  # 大小写不敏感
        "svc.internal.",  # 尾点容忍
    ],
)
def test_ssrf_blocks_private_tld(host):
    """内网保留域名后缀拒绝。"""
    with pytest.raises(SSRFError):
        check_host_sync(host)


def test_ssrf_blocks_hostname_resolving_to_private(monkeypatch):
    """域名解析到内网 IP 拒绝（防 DNS rebinding / 内网解析）。"""

    def fake_getaddrinfo(host, port):
        assert host == "evil.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFError):
        check_host_sync("evil.example")


def test_ssrf_allows_public_resolution(monkeypatch):
    """域名解析到公网 IP 放行。"""

    def fake_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    check_host_sync("example.com")  # 不应抛异常


def test_ssrf_blocks_dns_failure(monkeypatch):
    """域名解析失败拒绝。"""

    def fake_getaddrinfo(host, port):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFError):
        check_host_sync("no-such-host.invalid")
