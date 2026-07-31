# ============================================
# core/prompts/base.py - 提示词模板基类
# ============================================

from typing import Any


class PromptTemplate:
    """提示词模板"""

    def __init__(self, template: str):
        self._template = template

    def format(self, **kwargs: Any) -> str:
        return self._template.format(**kwargs)

    @property
    def raw(self) -> str:
        return self._template
