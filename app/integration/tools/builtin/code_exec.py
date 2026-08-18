"""
代码执行工具 - 在终端中执行命令
"""

import asyncio
from typing import Any, ClassVar

from ..base import BaseTool, ToolResult
from ..security import RiskLevel


class CodeExecTool(BaseTool):
    """终端命令执行工具"""

    _max_output_length: ClassVar[int] = 100_000

    @classmethod
    def register_config(
        cls, *, max_output_length: int = 100_000, **kwargs: Any
    ) -> None:
        """注入输出截断配置（由装配根调用，避免直接依赖 settings）。"""
        cls._max_output_length = max_output_length

    @property
    def risk_level(self) -> RiskLevel:
        """命令执行（潜在不可逆影响），L2。"""
        return RiskLevel.L2_DANGEROUS

    @property
    def category(self) -> str:
        return "code"

    @property
    def timeout(self) -> int:
        """工具自声明默认超时（秒）：编译 / 运行可较久，放宽至 60s。"""
        return 60

    @property
    def concurrency_safe(self) -> bool:
        """子进程执行非并发安全，串行化。"""
        return False

    @property
    def max_output_length(self) -> int:
        """结果截断上限（字符数），ResultProcessor 消费。"""
        return self._max_output_length

    # 禁止执行的危险命令前缀
    FORBIDDEN_PREFIXES: tuple[str, ...] = (
        "rm -rf /",
        "rm -rf /*",
        "rm -rf ~",
        "rm -rf .",
        "mkfs.",
        "dd if=",
        ":(){ :|:& };:",
        "> /dev/sda",
        "| shutdown",
        "| reboot",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init 0",
        "init 6",
        "mv /",
    )

    @property
    def name(self) -> str:
        return "code_exec"

    @property
    def description(self) -> str:
        return (
            "在系统终端中执行代码或命令并返回输出结果。"
            "当你需要运行 Python 脚本、Shell 命令、编译代码、"
            "或执行任何终端操作时使用此工具。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "待执行的终端命令，如 python main.py、ls -la、cd src && npm test",
                },
                "workdir": {
                    "type": "string",
                    "description": "命令执行的工作目录（绝对路径），留空则使用项目根目录",
                },
            },
            "required": ["command"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        """异步执行终端命令"""

        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")

        command: str = kwargs["command"].strip()
        workdir: str | None = kwargs.get("workdir")

        if not command:
            return ToolResult(success=False, content="", error="命令不能为空")

        # 安全检查：禁止危险命令
        command_lower = command.lower().strip()
        for prefix in self.FORBIDDEN_PREFIXES:
            if command_lower.startswith(prefix.lower()):
                return ToolResult(
                    success=False,
                    content="",
                    error=f"命令被安全策略拦截（高危操作）: {prefix}",
                )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
            )
        except FileNotFoundError as e:
            return ToolResult(
                success=False,
                content="",
                error=f"命令不存在或未找到可执行文件: {e!s}",
            )

        # 超时 / 取消时主动终止子进程，避免孤儿进程泄漏：
        # executor 外层 asyncio.wait_for 超时会对本协程注入 CancelledError，
        # 此处捕获后先 kill 子进程再重抛，保证进程与管道被回收。
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except TimeoutError:
            proc.kill()
            # kill 只是发信号，进程不会瞬间消失；
            # 同时子进程的 stdout/stderr 管道还处于打开状态。
            # 再次调用communicate()：等待进程真正退出、
            # 消费完管道缓冲区、关闭 IO 管道，避免文件句柄泄漏。
            await proc.communicate()  # kill 后再次 communicate 关闭管道并等待退出
            return ToolResult(
                success=False,
                content="",
                error=f"命令执行超时（{self.timeout} 秒）",
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.communicate()
            raise
        except Exception as e:  # noqa: BLE001
            # communicate 阶段异常：尽力终止子进程后归因，不留孤儿
            proc.kill()
            await proc.communicate()
            return ToolResult(
                success=False,
                content="",
                error=f"命令执行失败: {e!s}",
            )

        stdout_str = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_str = stderr.decode("utf-8", errors="replace") if stderr else ""

        # 结果截断由 ResultProcessor 统一处理（head+tail），此处返回完整内容
        # 构建返回内容
        parts = []
        if stdout_str:
            parts.append(stdout_str)
        if stderr_str:
            parts.append(f"--- stderr ---\n{stderr_str}")

        content = "".join(parts) if parts else "(无输出)"

        success = proc.returncode == 0
        return ToolResult(
            success=success,
            content=content,
            metadata={
                "return_code": proc.returncode,
                "command": command,
            },
        )
