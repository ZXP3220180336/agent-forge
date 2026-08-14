# ============================================
# deps.py - 依赖注入函数（原 dependencies.py）
# 与路由定义在同一包中，供路由函数使用
# ============================================

from fastapi import Header, HTTPException

from app.container import app_state
from app.application.context.context_manager import ContextManager
from app.application.session.session_manager import SessionManager
from app.application.task.task_service import TaskService
from app.integration.llm.llm_service import LLMService
from app.integration.tools.tool_service import ToolService


async def get_current_user(
    authorization: str = Header(None),
) -> str:
    """从 Token 中解析用户 ID（实际项目使用 JWT/OAuth）"""
    if not authorization:
        raise HTTPException(status_code=401, detail="未授权")
    # 模拟解析 token，返回 user_id
    # 实际项目中替换为 JWT 验证
    return "user_" + authorization[:8]


async def get_session_manager() -> SessionManager:
    """
    获取会话管理器实例（依赖注入）。

    返回全局单例，避免每个请求都创建新的实例。
    SessionManager 内部维护了 Redis 连接池和数据库连接池，
    这些资源应该在整个应用生命周期内复用。
    """
    if app_state.session_manager is None:
        raise RuntimeError(
            "SessionManager 尚未初始化。"
            "请确保在应用启动时调用了 app_state.initialize()。"
        )
    return app_state.session_manager


async def get_context_manager() -> ContextManager:
    """
    获取上下文管理器实例（依赖注入）。

    ContextManager 依赖 SessionManager 来获取历史消息，
    并依赖 tiktoken 编码器来计算 Token 数。
    """
    if app_state.context_manager is None:
        raise RuntimeError(
            "ContextManager 尚未初始化。"
            "请确保在应用启动时调用了 app_state.initialize()。"
        )
    return app_state.context_manager


async def get_llm_service() -> LLMService:
    """
    获取大模型服务实例（依赖注入）。

    LLMService 封装了 OpenAI SDK 的异步客户端，
    管理 API Key、Base URL 等配置。
    """
    if app_state.llm_service is None:
        raise RuntimeError(
            "LLMService 尚未初始化。请确保在应用启动时调用了 app_state.initialize()。"
        )
    return app_state.llm_service


async def get_tool_service() -> ToolService:
    """
    获取工具服务（依赖注入）。

    内置工具在 app_state.initialize() 时通过 init_default_tools() 注册到服务实例，
    ReActAgent 通过它获取工具定义并执行工具调用。
    """
    if app_state.tool_service is None:
        raise RuntimeError(
            "ToolService 尚未初始化。请确保在应用启动时调用了 app_state.initialize()。"
        )
    return app_state.tool_service


async def get_task_service() -> TaskService:
    """
    获取任务调度服务（依赖注入）。

    TaskService 用信号量限制并发 Agent 任务数（agent_max_concurrent_tasks），
    chat 路由通过它在任务级并发约束下运行 Agent。
    """
    if app_state.task_service is None:
        raise RuntimeError(
            "TaskService 尚未初始化。请确保在应用启动时调用了 app_state.initialize()。"
        )
    return app_state.task_service
