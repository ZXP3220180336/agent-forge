"""
文件操作工具 - 读写文件
"""

import os
from typing import Any

import aiofiles

from app.config import settings
from ..base import BaseTool, ToolResult


class ReadFileTool(BaseTool):
    """文件读取工具"""

    @property
    def name(self) -> str:
        return "readFile"

    @property
    def description(self) -> str:
        return "读取指定路径的文本文件内容。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "文本文件的绝对路径",
                }
            },
            "required": ["file_path"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        """读取文件内容"""

        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")

        try:
            async with aiofiles.open(kwargs["file_path"], encoding="utf-8") as file:
                content = await file.read()

            # 截断过大的文件内容
            max_len = settings.tool_max_output_length
            if len(content) > max_len:
                content = (
                    content[:max_len]
                    + f"\n...（内容已截断，共 {len(content)} 字符）"
                )

            return ToolResult(success=True, content=content)
        except FileNotFoundError:
            return ToolResult(
                success=False, content="", error=f"文件 '{kwargs['file_path']}' 未找到"
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"读取文件失败: {e!s}")


class WriteFileTool(BaseTool):
    """文件写入工具"""

    @property
    def name(self) -> str:
        return "writeFile"

    @property
    def description(self) -> str:
        return "将指定内容写入文本文件，如果文件不存在则创建。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "待写入内容的文件的绝对路径",
                },
                "content": {
                    "type": "string",
                    "description": "待写入的文件内容",
                },
            },
            "required": ["file_path", "content"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        """写入文件内容"""

        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")

        try:
            # 自动创建父目录
            dir_path = os.path.dirname(kwargs["file_path"])
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)

            async with aiofiles.open(
                kwargs["file_path"], "w", encoding="utf-8"
            ) as file:
                await file.write(kwargs["content"])
            return ToolResult(success=True, content=f"成功写入 '{kwargs['file_path']}'")
        except Exception as e:
            return ToolResult(success=False, content="", error=f"写入文件失败: {e!s}")
