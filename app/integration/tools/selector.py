"""工具选择器：从注册工具集中选出本次注入 LLM 的工具子集（预留接口）。

设计决策（见 ADR `adr/integration/tools/2026-08-17-six-component-alignment.md`）：
- 当前 10 个工具属小体量（<50），工业标准是全量注入 + LLM 原生 Function Calling
- 选择器只定义接口 + 默认全量实现，不做向量召回（>50 工具才需要）
- 预留方式：ToolService 构造期注入，get_openai_tools() 签名保持零参数
  （ToolGateway 协议不变，Agent 层无感知）
"""

from __future__ import annotations

from typing import Protocol

from app.integration.tools.base import BaseTool


class ToolSelector(Protocol):
    """选择器协议：从注册工具集中选出本次注入 LLM 的工具子集。"""

    def select(self, tools: list[BaseTool]) -> list[BaseTool]: ...


class DefaultToolSelector:
    """默认全量注入：小体量（<50）直接用 LLM 原生 Function Calling。

    工业级对照：>50 工具才需「向量召回粗排 + LLM 精排」。当前 10 个工具全量注入。
    未来扩展：实现 ToolSelector 协议并在 ToolService 构造期注入。
    """

    def select(self, tools: list[BaseTool]) -> list[BaseTool]:
        return tools
