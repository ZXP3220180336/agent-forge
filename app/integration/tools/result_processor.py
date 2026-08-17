"""工具结果处理器：统一 head+tail 截断 + 错误归一化。

设计决策（见 ADR `adr/integration/tools/2026-08-17-six-component-alignment.md`）：
- head+tail 默认 7:3（head_ratio=0.7）：LLM 最需要开头（上下文/指令）与
  结尾（code_exec 的 stderr/traceback 常在尾部），中间信息密度低
- 截断标记含原长度，供 agent 感知被移除内容
- 内置工具不再各自内联截断，统一在此处理（避免双重截断）
"""

from __future__ import annotations

from app.domain.ports.tool_gateway import ToolResult

DEFAULT_TRUNCATION_MARKER = "\n...（内容已截断，共 {original_len} 字符，仅保留首尾）\n"


class ResultProcessor:
    """统一结果处理：截断策略（head+tail）与错误归一化。"""

    def __init__(
        self,
        *,
        default_max_length: int = 100_000,
        head_ratio: float = 0.7,
        marker_template: str = DEFAULT_TRUNCATION_MARKER,
        max_error_length: int = 2_000,
    ) -> None:
        self._default_max_length = default_max_length
        self._head_ratio = head_ratio
        self._marker_template = marker_template
        self._max_error_length = max_error_length

    def truncate(
        self,
        content: str,
        *,
        max_length: int | None = None,
        head_ratio: float | None = None,
    ) -> str:
        """head+tail 截断：len ≤ max_length 原样返回；否则保留前 head + 后 tail。

        head = int(max_length * head_ratio)，tail = 余量，中间替换为截断标记。
        """
        limit = self._default_max_length if max_length is None else max_length
        if len(content) <= limit:
            return content

        ratio = self._head_ratio if head_ratio is None else head_ratio
        head_len = int(limit * ratio)
        tail_len = limit - head_len
        marker = self._marker_template.format(original_len=len(content))
        return content[:head_len] + marker + content[-tail_len:]

    def truncate_result(
        self, result: ToolResult, *, max_length: int | None = None
    ) -> None:
        """就地截断 result.content；截断发生时 metadata['truncated'] = True。"""
        original = result.content
        truncated = self.truncate(original, max_length=max_length)
        if truncated != original:
            result.content = truncated
            result.metadata = dict(result.metadata or {})
            result.metadata["truncated"] = True

    def normalize_error(self, error: str | None) -> str:
        """错误归一化：None→''；去首尾空白；压缩多余空行；超长截断（保留头部）。

        保留换行结构（traceback 可读性），只压缩连续空行。
        """
        if not error:
            return ""
        lines = [line.strip() for line in error.strip().splitlines() if line.strip()]
        cleaned = "\n".join(lines)
        if len(cleaned) > self._max_error_length:
            cleaned = cleaned[: self._max_error_length] + "...（错误过长已截断）"
        return cleaned
