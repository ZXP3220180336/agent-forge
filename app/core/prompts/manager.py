# ============================================
# core/prompts/manager.py - 提示词管理器
# ============================================


from .templates.system import SYSTEM_PROMPT
from .templates.tools import TOOL_FORMAT_PROMPT


class PromptManager:
    """
    提示词管理器
    负责组装不同场景下的完整提示词。
    """

    @staticmethod
    def build_system_prompt(tool_descriptions: str = "") -> str:
        """构建系统提示词"""
        prompt = SYSTEM_PROMPT
        if tool_descriptions:
            prompt += f"\n\n{TOOL_FORMAT_PROMPT.format(tools=tool_descriptions)}"
        return prompt
