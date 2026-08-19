"""
文件操作工具 - 读写文件
"""

import os
from typing import Any, ClassVar

import aiofiles

from ..base import BaseTool, ToolResult
from ..security import RiskLevel


def _normalize_allowed_dirs(allowed_dirs: tuple[str, ...]) -> tuple[str, ...]:
    """规范化白名单：abspath + normcase（Windows 路径大小写不敏感）。"""
    return tuple(os.path.normcase(os.path.abspath(d)) for d in allowed_dirs)


def _is_path_allowed(file_path: str, allowed_dirs: tuple[str, ...]) -> bool:
    """file_path 是否位于允许目录内（规范化比较，防 `..` 穿越 / 前缀误放行）。

    - 空白名单 = 拒绝所有（fail-closed，安全默认）
    - 允许 = 路径等于某允许目录，或是其子路径（`base + os.sep` 前缀避免 `/data` 误放行 `/database`）
    """
    if not allowed_dirs:
        return False
    candidate = os.path.normcase(os.path.abspath(file_path))
    return any(
        candidate == base or candidate.startswith(base + os.sep)
        for base in allowed_dirs
    )


class ReadFileTool(BaseTool):
    """文件读取工具"""

    _max_output_length: ClassVar[int] = 100_000
    _allowed_dirs: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def register_config(
        cls,
        *,
        max_output_length: int = 100_000,
        allowed_dirs: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> None:
        """注入内容截断与允许目录配置（由装配根调用，避免直接依赖 settings）。"""
        cls._max_output_length = max_output_length
        cls._allowed_dirs = _normalize_allowed_dirs(allowed_dirs)

    @property
    def risk_level(self) -> RiskLevel:
        """只读文件，L0。"""
        return RiskLevel.L0_READONLY

    @property
    def category(self) -> str:
        return "file"

    @property
    def timeout(self) -> int:
        """工具自声明默认超时（秒）：本地读快，收紧至 5s。"""
        return 5

    @property
    def max_output_length(self) -> int:
        """结果截断上限（字符数），ResultProcessor 消费。"""
        return self._max_output_length

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

        file_path: str = kwargs["file_path"]
        if not _is_path_allowed(file_path, self._allowed_dirs):
            return ToolResult(
                success=False,
                content="",
                error=f"文件路径不在允许目录内，拒绝访问: {file_path}",
            )

        try:
            # 大文件分段读取（head+tail），限制内存峰值；最终截断标记仍由 ResultProcessor 统一生成
            # UTF-8 中文最多 3 字节/字符，单段取 max_output_length×3 字节 ≈ 可容纳 max_output_length 字符
            size = os.path.getsize(file_path)
            max_bytes = self._max_output_length * 3
            async with aiofiles.open(file_path, "rb") as file:
                if size <= max_bytes * 2:
                    content = (await file.read()).decode("utf-8", errors="replace")
                else:
                    head = (await file.read(max_bytes)).decode("utf-8", errors="replace")
                    await file.seek(size - max_bytes)
                    tail = (await file.read(max_bytes)).decode("utf-8", errors="replace")
                    content = head + "\n...（文件过大，仅读取首尾）\n" + tail

            # 正常路径返回完整内容；大文件分段读取保留首尾，截断标记由 ResultProcessor 统一生成
            return ToolResult(success=True, content=content)
        except FileNotFoundError:
            return ToolResult(
                success=False, content="", error=f"文件 '{file_path}' 未找到"
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, content="", error=f"读取文件失败: {e!s}")


class WriteFileTool(BaseTool):
    """文件写入工具"""

    _allowed_dirs: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def register_config(
        cls, *, allowed_dirs: tuple[str, ...] = (), **kwargs: Any
    ) -> None:
        """注入允许目录配置（由装配根调用，避免直接依赖 settings）。"""
        cls._allowed_dirs = _normalize_allowed_dirs(allowed_dirs)

    @property
    def risk_level(self) -> RiskLevel:
        """写文件（修改磁盘），L1。"""
        return RiskLevel.L1_WRITE

    @property
    def category(self) -> str:
        return "file"

    @property
    def timeout(self) -> int:
        """工具自声明默认超时（秒）：本地写快，收紧至 5s。"""
        return 5

    @property
    def concurrency_safe(self) -> bool:
        """写文件存在并发覆盖风险，串行化。"""
        return False

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

        file_path: str = kwargs["file_path"]
        if not _is_path_allowed(file_path, self._allowed_dirs):
            return ToolResult(
                success=False,
                content="",
                error=f"文件路径不在允许目录内，拒绝访问: {file_path}",
            )

        try:
            # 自动创建父目录
            dir_path = os.path.dirname(file_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)

            async with aiofiles.open(file_path, "w", encoding="utf-8") as file:
                await file.write(kwargs["content"])
            return ToolResult(success=True, content=f"成功写入 '{file_path}'")
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, content="", error=f"写入文件失败: {e!s}")
