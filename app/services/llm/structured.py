"""
StructuredOutput — 结构化输出支持

职责：
    1. 根据 JSON Schema 从 LLM 输出中提取结构化数据
    2. 优先使用原生 response_format（JSON Schema）
    3. 降级：模型不支持时使用 prompt 约束

使用方式（统一入口为 LLMService.generate_structured，委托本类）：
    result = await StructuredOutput.extract(
        llm_service=llm_service,
        messages=[{"role": "user", "content": "张三去了北京"}],
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
    )
    # → {"name": "张三"}
"""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import ValidationError, validate

from app.utils.logger import get_logger

logger = get_logger("llm.structured")


class StructuredExtractionError(Exception):
    """结构化提取的 API 边界失败基类（截断/拒答），短路不进入降级链。"""


class StructuredTruncationError(StructuredExtractionError):
    """输出被 max_tokens 截断，扩 token 重试后仍不完整。"""


class StructuredRefusalError(StructuredExtractionError):
    """模型拒答（内容安全策略触发），不强行 repair。"""


# 截断/拒答的 finish_reason 判定集合（问题 2 三态检查）。
# DeepSeek 额外有 insufficient_system_resource（推理资源中断）；Anthropic 用 max_tokens。
_TRUNCATED_REASONS = frozenset(["length", "max_tokens", "insufficient_system_resource"])
_REFUSAL_REASONS = frozenset(["content_filter"])


class StructuredOutput:
    """
    结构化输出提取器（三级降级实现载体）。

    优先使用 OpenAI 原生 JSON Schema（response_format），
    兜底使用 JSON Mode、prompt 约束 + 正则提取。
    统一对外入口：LLMService.generate_structured()（本类为内部实现）。
    """

    @staticmethod
    def build_json_schema_request(
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """
        构建 response_format 参数。

        当模型支持 strict=True 时启用，否则降级为普通 JSON mode。

        Args:
            schema: JSON Schema

        Returns:
            {"type": "json_schema" | "json_object", "json_schema": {...}}
        """
        # 尝试 native JSON Schema
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "strict": True,
                "schema": schema,
            },
        }

    @staticmethod
    def build_json_mode_request() -> dict[str, str]:
        """构建普通 JSON mode 请求参数（无 Schema 约束）。"""
        return {"type": "json_object"}

    @staticmethod
    async def extract(
        llm_service: Any,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
    ) -> dict[str, Any] | None:
        """
        根据 JSON Schema 从消息中提取结构化数据（三级降级）。

        问题 2 语义：截断/拒答（StructuredExtractionError）短路返回 None——
        不进入降级链（截断与降级正交、拒答是策略信号，降级无益只会浪费调用）。

        Args:
            llm_service: LLMService 实例（generate 代理）
            messages: 完整消息列表（调用方构建）
            schema: JSON Schema 定义
            model_key: 使用的模型标识（默认 fast，低延迟低成本）

        Returns:
            解析后的 dict，三级均失败返回 None
        """
        # 先用原生 JSON Schema
        response_format = StructuredOutput.build_json_schema_request(schema)
        try:
            result = await StructuredOutput._try_extract(
                llm_service,
                messages,
                response_format,
                model_key,
                schema=schema,
            )
        except StructuredExtractionError:
            return None  # 截断/拒答短路，不降级
        if result is not None:
            return result

        # 降级：普通 JSON mode
        response_format = StructuredOutput.build_json_mode_request()
        try:
            result = await StructuredOutput._try_extract(
                llm_service,
                messages,
                response_format,
                model_key,
                schema=schema,
            )
        except StructuredExtractionError:
            return None  # 截断/拒答短路，不降级
        if result is not None:
            return result

        # 最终降级：纯 prompt 约束 + 正则提取
        try:
            return await StructuredOutput._fallback_extract(
                llm_service,
                messages,
                model_key,
                schema=schema,
            )
        except StructuredExtractionError:
            return None  # 截断/拒答短路

    @staticmethod
    def _validate_schema(parsed: dict[str, Any], schema: dict[str, Any]) -> bool:
        """按 JSON Schema 校验解析结果。

        返回 False 时校验失败原因已记录日志，调用方应触发降级。
        这是「模型返回不能直接进业务」的本地校验一环——strict 只锁结构，
        minimum/maximum/pattern 等值约束与 refusal/截断绕过，都靠这层兜底。
        """
        try:
            validate(instance=parsed, schema=schema)
            return True
        except ValidationError as e:
            logger.warning(
                "结构化输出 Schema 校验失败: %s (schema=%s, parsed=%s)",
                e.message,
                json.dumps(schema, ensure_ascii=False),
                json.dumps(parsed, ensure_ascii=False),
            )
            return False
        except Exception as e:  # schema 本身非法（非标准关键字等）
            logger.error(
                "Schema 校验器异常（schema 可能非法）: %s", e,
            )
            return False

    @staticmethod
    def _classify_result(result: Any) -> str:
        """分类结构化响应失败类型（解析前 API 边界检查，问题 2）。

        检查顺序：refusal 字段 → finish_reason 拒答/过滤 → finish_reason 截断 → content 空。
        返回 "ok" / "truncated" / "refusal"。
        """
        if getattr(result, "refusal", None):
            return "refusal"
        fr = result.finish_reason
        if fr in _REFUSAL_REASONS:
            return "refusal"
        if fr in _TRUNCATED_REASONS:
            return "truncated"
        if not result.content:
            # content 空 + 正常结束 = 拒答/空回（DeepSeek 无 refusal 字段形态）
            return "refusal"
        return "ok"

    @staticmethod
    async def _try_extract(
        llm_service: Any,
        messages: list[dict],
        response_format: dict,
        model_key: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """尝试用指定 response_format 提取（解析前做三态检查）。

        问题 2 语义：
        - 截断（length/max_tokens/insufficient_system_resource）：本层扩 max_tokens
          重试 1 次；重试后仍失败抛 StructuredTruncationError（短路，不降级）
        - 拒答（refusal/content_filter/content 空）：抛 StructuredRefusalError
          （短路，不强行 repair）
        - 正常：解析 + Schema 校验，普通失败返回 None（触发降级）

        Args:
            schema: 传入则对解析结果做 Schema 校验（校验失败返回 None 触发降级）
        """
        try:
            result = await llm_service.generate(
                messages=messages,
                temperature=0,
                max_tokens=2048,
                response_format=response_format,
                model_key=model_key,
            )
        except Exception:
            return None  # 下游失败（可靠性层已重试），降级
        if result is None:
            return None

        failure = StructuredOutput._classify_result(result)
        if failure == "truncated":
            logger.warning(
                "结构化输出截断: finish_reason=%s, 扩 max_tokens 重试 1 次",
                result.finish_reason,
            )
            try:
                retry = await llm_service.generate(
                    messages=messages,
                    temperature=0,
                    max_tokens=4096,
                    response_format=response_format,
                    model_key=model_key,
                )
            except Exception:
                retry = None
            if retry is not None and StructuredOutput._classify_result(retry) == "ok":
                result = retry
            elif retry is not None and StructuredOutput._classify_result(
                retry
            ) == "refusal":
                raise StructuredRefusalError(
                    f"截断重试后拒答: finish_reason={retry.finish_reason}"
                )
            else:
                raise StructuredTruncationError(
                    f"扩 token 重试后仍截断: finish_reason="
                    f"{retry.finish_reason if retry else 'None'}"
                )
        elif failure == "refusal":
            logger.warning(
                "结构化输出拒答: refusal=%r, finish_reason=%s，短路不 repair",
                getattr(result, "refusal", None),
                result.finish_reason,
            )
            raise StructuredRefusalError(
                f"refusal={getattr(result, 'refusal', None)!r}, "
                f"finish_reason={result.finish_reason}"
            )

        # 正常：解析 + 校验
        try:
            parsed = json.loads(result.content)
        except Exception:
            return None  # 解析失败 → 降级
        if not isinstance(parsed, dict):
            return None
        if schema is not None and not StructuredOutput._validate_schema(parsed, schema):
            return None  # 校验失败 → 降级
        return parsed

    @staticmethod
    async def _fallback_extract(
        llm_service: Any,
        messages: list[dict],
        model_key: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """纯 prompt 约束降级方案（同样做三态检查，截断/拒答短路）。

        第三级到头了无降级可走：截断不扩 token 重试（纯 prompt 约束重试收益不定），
        拒答/截断记日志后抛异常短路。
        """
        try:
            result = await llm_service.generate(
                messages=messages,
                temperature=0,
                max_tokens=2048,
                model_key=model_key,
            )
        except Exception:
            return None
        if result is None:
            return None

        failure = StructuredOutput._classify_result(result)
        if failure == "truncated":
            logger.warning(
                "结构化输出截断（fallback）: finish_reason=%s，短路不降级",
                result.finish_reason,
            )
            raise StructuredTruncationError(f"finish_reason={result.finish_reason}")
        if failure == "refusal":
            logger.warning(
                "结构化输出拒答（fallback）: refusal=%r, finish_reason=%s，短路不 repair",
                getattr(result, "refusal", None),
                result.finish_reason,
            )
            raise StructuredRefusalError(
                f"refusal={getattr(result, 'refusal', None)!r}, "
                f"finish_reason={result.finish_reason}"
            )

        # 尝试提取 JSON 块
        try:
            content = result.content.strip()
            # 移除 markdown 代码块标记
            content = re.sub(
                r"^```(?:json)?\s*",
                "",
                content,
                flags=re.MULTILINE,
            )
            content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
            parsed = json.loads(content)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        if schema is not None and not StructuredOutput._validate_schema(parsed, schema):
            return None
        return parsed
