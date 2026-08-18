"""
工具基类定义
所有工具都应继承此基类
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.domain.ports.tool_gateway import ToolResult
from app.integration.tools.security import RiskLevel
from app.integration.tools.validator import ParameterValidator

# 模块级校验器单例（无状态）；与 executor 注入实例配置恒等（reject_unknown=True）
_validator = ParameterValidator()


class BaseTool(ABC):
    """
    工具抽象基类

    所有工具必须实现以下方法：
    - name: 工具名称（唯一标识）
    - description: 工具描述
    - parameters: 工具参数 JSON Schema
    - execute: 工具执行逻辑
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称（唯一标识）"""

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述（用于 LLM 理解工具功能）"""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """工具参数 JSON Schema"""

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """
        执行工具逻辑

        Args:
            **kwargs: 工具参数

        Returns:
            ToolResult: 执行结果
        """

    @classmethod
    def register_config(cls, **kwargs: Any) -> None:
        """可选：由装配根注入工具运行配置（默认无操作，子类按需覆盖）。

        内置工具（SearchTool / WebBrowseTool 等）各自实现此方法，
        经装配根调用，避免直接依赖 settings。
        """

    # ===== 元数据（分级标注 + 审计用，仅标注不拦截） =====

    @property
    def risk_level(self) -> RiskLevel:
        """风险分级（L0 只读 / L1 写 / L2 危险 / L3 禁用）。默认最安全 L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        """功能域（search / file / code / web / ...），供按域查询。"""
        return "general"

    @property
    def concurrency_safe(self) -> bool:
        """是否允许自身并发执行（写类 / 子进程类工具应为 False → 串行化）。"""
        return True

    @property
    def requires_approval(self) -> bool:
        """是否需人工审批（HITL）。由 executor 执行前经 ApprovalGate 确认，默认放行。"""
        return False

    @property
    def max_output_length(self) -> int:
        """结果截断上限（字符数），ResultProcessor 消费。默认 100_000。"""
        return 100_000

    @property
    def timeout(self) -> int | None:
        """工具自声明默认超时（秒）。None = 沿用全局配置；调用方显式传入可覆盖。"""
        return None

    # ===== 生命周期钩子（对齐工业热插拔：加载 / 卸载 / 健康检查） =====

    async def on_load(self) -> None:
        """加载完成后初始化（建立连接 / 加载配置）。默认无操作。

        由外部工具加载器在实例化并注册前调用；失败 → 该工具跳过并回滚。
        """

    async def on_unload(self) -> None:
        """卸载前释放资源（连接 / 子进程 / 定时器）。默认无操作。

        由外部工具加载器在注销前调用；异常不影响卸载流程。
        """

    async def health_check(self) -> bool:
        """健康检查，返回可用性状态。默认可用（接口预留，供未来巡检隔离）。"""
        return True

    # ===== Schema 导出 =====

    def to_openai_tool(self) -> dict[str, Any]:
        """
        转换为 OpenAI Tool 格式

        Returns:
            OpenAI Tool Schema
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_openai_response(self) -> dict[str, Any]:
        """
        转换为 OpenAI Response 格式（用于 tool_calls 响应）

        Returns:
            OpenAI Response Schema
        """
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    # ===== 参数校验（委托 jsonschema 校验器） =====

    def validate_parameters(self, **kwargs) -> bool:
        """
        参数是否符合 Schema（jsonschema 完整校验：必填 / 未知 / 类型 / 枚举 / 范围）。

        Args:
            **kwargs: 工具参数

        Returns:
            bool: 参数是否有效
        """
        return not self.validation_issues(**kwargs)

    def validation_issues(self, **kwargs) -> list[str]:
        """返回参数校验问题（中文描述列表）；空列表 = 通过。

        供 executor 构造可归因的错误信息（LLM 下一轮据此修正）。
        """
        issues = _validator.validate(self.parameters, kwargs)
        return [issue.message for issue in issues]
