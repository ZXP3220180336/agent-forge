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

import copy
import json
import re
from typing import Any

from jsonschema import Draft7Validator, ValidationError, validate

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

# 错误回喂重试（问题 3）：每级校验失败回喂上限（额外尝试次数，最多 3 次请求）。
# 工业共识 2~3 次；首次修正成功率最高，之后陡降。
_REASK_MAX_RETRIES = 2

_REASK_TEMPLATE = (
    "你的上一次输出未通过 JSON Schema 校验，具体错误如下：\n{errors}\n"
    "请根据错误修正，只输出符合 schema 的 JSON 对象，"
    "不要 markdown 代码块、不要额外解释。"
)


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
    def _enforce_no_extra_fields(schema: dict[str, Any]) -> dict[str, Any]:
        """深拷贝并递归补全 `additionalProperties: false`（问题 4）。

        - 深拷贝：不污染调用方 schema（默认补全发生在副本上）
        - 递归：对每个 object 节点补 `additionalProperties:false`，拒绝模型扩展字段
        - 显式尊重：调用方已写 `true` 的保持 `true`（不覆盖显式允许扩展的意图）

        意义：减少模型自作主张扩展接口（如业务不需要的 `user_emotion` 混入）。
        配合 Pydantic 侧 `extra="forbid"`（业务层）双保险。
        """
        new_schema = copy.deepcopy(schema)
        stack = [new_schema]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            if node.get("type") == "object" and "additionalProperties" not in node:
                node["additionalProperties"] = False
            # 递归属性定义与子结构
            for value in node.values():
                if isinstance(value, dict):
                    stack.append(value)
                elif isinstance(value, list):
                    stack.extend(v for v in value if isinstance(v, dict))
        return new_schema

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
        # 问题 4：递归补全 additionalProperties:false（深拷贝，不污染调用方 schema）。
        # 默认拒绝额外字段，模型无法扩展接口混入业务不需要的字段。
        schema = StructuredOutput._enforce_no_extra_fields(schema)

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
        except Exception:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
                retry = None
            if retry is not None and StructuredOutput._classify_result(retry) == "ok":
                result = retry
            elif (
                retry is not None
                and StructuredOutput._classify_result(retry) == "refusal"
            ):
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

        # 正常：解析 + 校验（错误回喂重试，问题 3）
        # 同一 response_format（同一级约束）下重试：把具体校验错误回喂模型修正。
        # 回喂耗尽 → 返回 None 触发降级（与现有降级链无缝衔接）。
        content = result.content
        for _ in range(_REASK_MAX_RETRIES):
            parsed, errors = StructuredOutput._parse_and_validate(content, schema)
            if parsed is not None:
                return parsed
            # 回喂：clone + assistant 失败输出 + user 错误反馈（不污染调用方 messages）
            retry = await llm_service.generate(
                messages=StructuredOutput._build_reask_messages(
                    messages, content, "\n".join(errors)
                ),
                temperature=0,
                max_tokens=2048,
                response_format=response_format,
                model_key=model_key,
            )
            if retry is None:
                return None  # 下游失败 → 降级
            failure = StructuredOutput._classify_result(retry)
            if failure == "truncated":
                return None  # 回喂循环内截断 → 降级（不与扩 token 逻辑组合，防 token 爆炸）
            if failure == "refusal":
                raise StructuredRefusalError(
                    f"回喂重试后拒答: finish_reason={retry.finish_reason}"
                )
            content = retry.content
        return None  # 回喂耗尽 → 降级

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
    def _parse_and_validate(
        content: str,
        schema: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """解析 + 校验，返回 (结果, 错误列表)。

        返回的二元组供回喂循环（问题 3）使用：
        - parsed 非 None = 成功（校验通过），错误列表空
        - parsed 为 None = 失败，errors 携带人话错误（供回喂 / 记日志）
        """
        try:
            parsed = json.loads(content)
        except Exception as e:
            return None, [f"- 顶层 JSON 解析失败：{e}"]

        if not isinstance(parsed, dict):
            return None, ["- 顶层不是 JSON 对象（应为 dict）"]

        if schema is not None:
            errors = StructuredOutput._collect_schema_errors(parsed, schema)
            if errors:
                return None, errors

        return parsed, []

    @staticmethod
    def _collect_schema_errors(
        parsed: dict[str, Any], schema: dict[str, Any]
    ) -> list[str]:
        """收集全部 Schema 校验错误，格式化为「字段路径: message」的人话（问题 3）。

        一次收集全部错误（iter_errors 全量，非第一条），让模型一次改完。
        返回空列表 = 校验通过。
        """
        errors = []
        for e in Draft7Validator(schema).iter_errors(parsed):
            path = "/".join(str(p) for p in e.absolute_path) or "<root>"
            errors.append(f"- 字段 `{path}`：{e.message}")
        return errors

    @staticmethod
    def _build_reask_messages(
        messages: list[dict],
        raw_content: str,
        error_text: str,
    ) -> list[dict]:
        """构造错误回喂的消息（问题 3）：clone + 保留上次失败输出 + 末尾追加反馈。

        - clone：`[dict(m) for m in messages]` 浅拷贝，绝不污染调用方 messages
        - 保留失败输出：assistant 消息留在历史里，让模型看到自己错在哪（self-correction 关键）
        - 末尾追加 user 消息：具体错误 + 修正指令（一次性指令，非 system 恒定规则）
        """
        new_messages = [dict(m) for m in messages]
        if raw_content:
            new_messages.append({"role": "assistant", "content": raw_content})
        new_messages.append(
            {"role": "user", "content": _REASK_TEMPLATE.format(errors=error_text)}
        )
        return new_messages

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
                "Schema 校验器异常（schema 可能非法）: %s",
                e,
            )
            return False
