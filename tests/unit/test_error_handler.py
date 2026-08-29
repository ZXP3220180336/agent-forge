"""app/api/middleware/error_handler.py 单元测试：AppError → HTTP 状态 + 统一信封。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware.error_handler import register_error_handlers
from app.shared.exceptions import (
    CircuitBreakerOpenError,
    ForbiddenError,
    NotFoundError,
    ParameterValidationError,
    SSRFError,
    StructuredRefusalError,
    UnauthorizedError,
)


@pytest.fixture
def client():
    """注册 error_handler 的最小 FastAPI 应用（各路由抛不同 AppError）。"""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/unauthorized")
    async def unauthorized():
        raise UnauthorizedError("未授权")

    @app.get("/not_found")
    async def not_found():
        raise NotFoundError("会话不存在")

    @app.get("/forbidden")
    async def forbidden():
        raise ForbiddenError("无权访问")

    @app.get("/validation")
    async def validation():
        raise ParameterValidationError("参数错误")

    @app.get("/circuit")
    async def circuit():
        raise CircuitBreakerOpenError("熔断开启")

    @app.get("/refusal")
    async def refusal():
        raise StructuredRefusalError("拒答")

    @app.get("/ssrf")
    async def ssrf():
        raise SSRFError("SSRF 拦截")

    return TestClient(app)


def test_unauthorized(client):
    resp = client.get("/unauthorized")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "UNAUTHORIZED"
    assert body["message"] == "未授权"


def test_not_found(client):
    resp = client.get("/not_found")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
    assert resp.json()["message"] == "会话不存在"


def test_forbidden(client):
    resp = client.get("/forbidden")
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


def test_validation(client):
    resp = client.get("/validation")
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION"


def test_circuit_breaker(client):
    resp = client.get("/circuit")
    assert resp.status_code == 503
    assert resp.json()["code"] == "CIRCUIT_OPEN"


def test_llm_refusal(client):
    resp = client.get("/refusal")
    assert resp.status_code == 502
    assert resp.json()["code"] == "LLM_REFUSAL"


def test_ssrf(client):
    resp = client.get("/ssrf")
    assert resp.status_code == 400
    assert resp.json()["code"] == "SSRF_BLOCKED"
