"""工具参数校验器：jsonschema 严格校验 + 错误归因（供 LLM 下一轮修正）。

设计决策（见 ADR `adr/integration/tools/2026-08-17-jsonschema-strict-validation.md`）：
- 用 Draft202012Validator.iter_errors 收集全部错误，一次反馈给 LLM
  （比 validate() 只报首个更利于下一轮全部修正，减少归因往返）
- reject_unknown=True 时校验前给 schema 包一层 additionalProperties: False
- 不做类型转换：严格 fail-fast，让 LLM 收到「类型应为 X」后自我纠正
  （强制转换会掩盖 LLM 输出质量问题，归因闭环是「失败 → 明确错误 → LLM 重试」）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator


@dataclass(frozen=True)
class ValidationIssue:
    """单条参数校验问题（中文友好描述，供 LLM 归因）。"""

    message: str  # 完整中文描述（含字段名），如 "缺少必填参数 'file_path'"


class ParameterValidationError(ValueError):
    """参数校验失败（携带归因错误信息）。"""


# Python 类型名 → JSON Schema 类型名（错误信息语义一致）
_PY_TO_JSONSCHEMA_TYPE = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
    "NoneType": "null",
}


class ParameterValidator:
    """JSON Schema 严格校验器。"""

    def __init__(self, *, reject_unknown: bool = True) -> None:
        self._reject_unknown = reject_unknown

    def validate(
        self, schema: dict[str, Any], parameters: dict[str, Any]
    ) -> list[ValidationIssue]:
        """全量校验参数；返回全部问题（空列表 = 通过）。不做类型转换。"""
        effective = self._with_reject_unknown(schema)
        return [
            ValidationIssue(message=self._map_error(error))
            for error in Draft202012Validator(effective).iter_errors(parameters)
        ]

    def format_issues(self, issues: list[ValidationIssue]) -> str:
        """拼接全部问题为分号分隔的一句话（供 executor 写入错误信息）。"""
        return "; ".join(i.message for i in issues)

    def validate_or_raise(
        self, schema: dict[str, Any], parameters: dict[str, Any]
    ) -> None:
        """有错则抛 ParameterValidationError（供需要异常语义的调用方）。"""
        issues = self.validate(schema, parameters)
        if issues:
            raise ParameterValidationError(self.format_issues(issues))

    def _with_reject_unknown(self, schema: dict[str, Any]) -> dict[str, Any]:
        """reject_unknown=True 时包一层 additionalProperties: False。"""
        if not self._reject_unknown:
            return schema
        wrapped = dict(schema)
        wrapped["additionalProperties"] = False
        return wrapped

    def _map_error(self, error: Any) -> str:
        """按 jsonschema validator 类型映射中文归因模板。"""
        validator = error.validator

        if validator == "required":
            field = self._extract_quoted(error.message)
            return f"缺少必填参数 '{field}'"

        if validator == "type":
            field = self._field_name(error)
            actual = _PY_TO_JSONSCHEMA_TYPE.get(
                type(error.instance).__name__, type(error.instance).__name__
            )
            return f"参数 '{field}' 类型应为 {error.validator_value}，实际为 {actual}"

        if validator == "enum":
            field = self._field_name(error)
            options = ", ".join(repr(v) for v in error.validator_value)
            return f"参数 '{field}' 必须是 [{options}] 之一，实际为 {error.instance!r}"

        if validator == "additionalProperties":
            field = self._extract_quoted(error.message)
            return f"参数 '{field}' 不在 schema 允许范围内"
        # 兜底：minimum / maximum / minLength / maxLength / pattern / const 等

        field = self._field_name(error)
        return f"参数 '{field}' {error.message}"

    def _field_name(self, error: Any) -> str:
        """从 error.path 拼完整路径（嵌套字段保留父路径，对 LLM 归因更精确）；无 path 兜底 '参数'。"""
        if error.path:
            return ".".join(str(p) for p in error.path)
        return "参数"

    def _extract_quoted(self, message: str) -> str:
        """从 jsonschema 错误消息中提取第一个单引号字段名（如 `'file_path' is a required property`）。"""
        match = re.search(r"'([^']+)'", message)
        return match.group(1) if match else "参数"
