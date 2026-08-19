"""
代码执行工具 - 在终端中执行命令
"""

import asyncio
import locale
from typing import Any, ClassVar

from ..base import BaseTool, ToolResult
from ..security import RiskLevel


async def _read_stream_capped(stream: asyncio.StreamReader | None, cap: int) -> bytes:
    """流式读取流：保留前 cap 字节，超出部分丢弃（继续 drain 防子进程管道阻塞）。

    内存峰值 ≈ cap（而非子进程全部输出）；输出过长时保留头部信息。
    """
    if stream is None:
        return b""
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if total < cap:
            take = min(len(chunk), cap - total)
            parts.append(chunk[:take])
            total += take
    return b"".join(parts)


def _decode_output(raw: bytes) -> str:
    """解码子进程输出：优先 UTF-8（现代工具 / Python 脚本），非法字节回退系统 locale 编码。

    Windows 中文环境 `locale.getpreferredencoding(False)` 返回 cp936（GBK），
    匹配 cmd.exe 系统命令（`type 报告.txt` / 良率日志）的默认输出编码。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(encoding, errors="replace")


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
        # stdout/stderr 流式读取限制内存峰值（保留前部 + drain 防阻塞）。
        cap_bytes = self._max_output_length * 3  # UTF-8 中文最多 3 字节/字符
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(
                    _read_stream_capped(proc.stdout, cap_bytes),
                    _read_stream_capped(proc.stderr, cap_bytes),
                ),
                timeout=self.timeout,
            )
            returncode = await proc.wait()  # 流式读后等待进程退出，确保 returncode 已填充
        except TimeoutError:
            proc.kill()
            await proc.wait()  # kill 后等待进程退出，回收句柄
            return ToolResult(
                success=False,
                content="",
                error=f"命令执行超时（{self.timeout} 秒）",
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        except Exception as e:  # noqa: BLE001
            # 读取阶段异常：尽力终止子进程后归因，不留孤儿
            proc.kill()
            await proc.wait()
            return ToolResult(
                success=False,
                content="",
                error=f"命令执行失败: {e!s}",
            )

        stdout_str = _decode_output(stdout)
        stderr_str = _decode_output(stderr)

        # 结果截断由 ResultProcessor 统一处理（head+tail）；流式读取已限制输出量，此处拼接返回
        # 构建返回内容
        parts = []
        if stdout_str:
            parts.append(stdout_str)
        if stderr_str:
            parts.append(f"--- stderr ---\n{stderr_str}")

        content = "".join(parts) if parts else "(无输出)"

        success = returncode == 0
        return ToolResult(
            success=success,
            content=content,
            metadata={
                "return_code": returncode,
                "command": command,
            },
        )
