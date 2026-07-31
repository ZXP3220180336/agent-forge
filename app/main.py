# ============================================
# main.py - FastAPI 应用入口
# ============================================

import os
import sys
from contextlib import asynccontextmanager

# 将项目根目录加入 sys.path，支持 python app/main.py 直接运行
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# Windows 控制台默认 GBK 编码，统一切换为 UTF-8，
# 避免日志中的符号（⚠✓ 等）触发 UnicodeEncodeError 导致启动崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import chat_router, session_router
from app.app_state import app_state


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理。

    - 启动时：初始化所有服务实例
    - 关闭时：清理所有资源
    """
    # ===== 启动阶段 =====
    print("正在初始化应用服务...")
    await app_state.initialize()
    print("应用初始化完成")

    yield  # 应用运行中

    # ===== 关闭阶段 =====
    print("正在关闭应用服务...")
    await app_state.shutdown()
    print("应用已关闭")


# 创建 FastAPI 应用，注册生命周期
app = FastAPI(
    title="AI 对话助手 API",
    description="支持多轮对话与深度思考的流式聊天 API",
    version="1.0.0",
    lifespan=lifespan,
)


# 添加 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== 注册 API 路由 =====
app.include_router(chat_router)
app.include_router(session_router)


# ===== 后端 API 健康检查 =====
@app.get("/api/health")
async def health_check():
    """健康检查接口"""
    return {"status": "ok", "version": "1.0.0"}


# ===== 挂载静态文件 =====
STATIC_DIR = os.path.join(_root, "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    print(f"警告：静态文件目录 {STATIC_DIR} 不存在，前端页面不可用")


# ===== SPA 回退中间件（解决刷新 404） =====
@app.middleware("http")
async def spa_fallback(request, call_next):
    response = await call_next(request)

    if response.status_code == 404 and not request.url.path.startswith("/api/"):
        index_path = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path)

    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
