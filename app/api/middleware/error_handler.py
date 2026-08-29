"""API 层异常处理（Phase D error_handler）：AppError → HTTP 状态 + 统一信封。

把统一异常树（AppError）在对外边界翻译为 HTTP 状态 + `{code, message, details}`
信封。业务码（code）是响应体契约，HTTP 状态反映通信语义（二者解耦，对齐 DRF 实践）。
在 main.py 调用 `register_error_handlers(app)` 注册。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.shared.exceptions import AppError, AppErrorCode

# AppErrorCode → HTTP 状态映射（业务码与 HTTP 状态解耦，此处是边界翻译表）
_CODE_TO_STATUS: dict[AppErrorCode, int] = {
    AppErrorCode.UNAUTHORIZED: 401,
    AppErrorCode.NOT_FOUND: 404,
    AppErrorCode.FORBIDDEN: 403,
    AppErrorCode.VALIDATION: 400,
    AppErrorCode.LLM_TOOL_CALL: 400,
    AppErrorCode.SSRF_BLOCKED: 400,
    AppErrorCode.LLM_REFUSAL: 502,
    AppErrorCode.LLM_TRUNCATED: 502,
    AppErrorCode.CIRCUIT_OPEN: 503,
    AppErrorCode.INTERNAL: 500,
}


def _status_for(code: AppErrorCode) -> int:
    """AppErrorCode → HTTP 状态（未映射兜底 500）。"""
    return _CODE_TO_STATUS.get(code, 500)


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """AppError → 统一信封（业务码 + 可读消息 + 细节）。

    第二参按 `Exception` 标注（`Starlette.add_exception_handler` 签名要求宽类型）；
    注册时已限定 AppError，运行时恒为 AppError，此处断言收紧。
    """
    assert isinstance(exc, AppError), (
        f"handler 仅处理 AppError，收到 {type(exc).__name__}"
    )
    return JSONResponse(
        status_code=_status_for(exc.code),
        content={
            "code": exc.code.value,
            "message": exc.message,
            "details": None,
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """在 FastAPI 应用上注册统一异常处理。"""
    app.add_exception_handler(AppError, app_error_handler)
