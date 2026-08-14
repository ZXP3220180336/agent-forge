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

from app.integration.llm.retry import ErrorCategory, classify_error
from app.utils.logger import get_logger

logger = get_logger("llm.structured")


class StructuredExtractionError(Exception):
    """结构化提取的 API 边界失败基类（截断/拒答/工具调用），短路不进入降级链。"""


class StructuredTruncationError(StructuredExtractionError):
    """输出被 max_tokens 截断，扩 token 重试后仍不完整。"""


class StructuredRefusalError(StructuredExtractionError):
    """模型拒答（内容安全策略触发），不强行 repair。"""


class StructuredToolCallError(StructuredExtractionError):
    """模型选择调用工具而非输出 JSON（finish_reason=tool_calls）。

    与截断/拒答同级：模型已明确放弃输出结构化数据，降级到更宽松的约束
    （JSON mode / 纯 prompt）对「模型要调用工具」无意义——反复降级只会
    浪费调用。短路不进入降级链，抛给调用方按工具调用处理。
    """


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


def _build_json_schema_request(
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


def _build_json_mode_request() -> dict[str, str]:
    """构建普通 JSON mode 请求参数（无 Schema 约束）。"""
    return {"type": "json_object"}


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
        # 匹配 object：type 单值 "object" 或数组含 "object"（draft-07 可空写法 ["object","null"]）
        node_type = node.get("type")
        is_object = node_type == "object" or (
            isinstance(node_type, list) and "object" in node_type
        )
        if is_object and "additionalProperties" not in node:
            node["additionalProperties"] = False
        # 递归属性定义与子结构
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(v for v in value if isinstance(v, dict))
    return new_schema


def _try_parse_json(
    content: str,
    schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """解析 JSON 并校验 schema；任一失败返回 None（供第三级渐进提取复用）。"""
    try:
        parsed = json.loads(content)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(parsed, dict):
        return None
    if schema is not None and not _validate_schema(parsed, schema):
        return None
    return parsed


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
    except Exception as e:  # noqa: BLE001
        return None, [f"- 顶层 JSON 解析失败：{e}"]

    if not isinstance(parsed, dict):
        return None, ["- 顶层不是 JSON 对象（应为 dict）"]

    if schema is not None:
        errors = _collect_schema_errors(parsed, schema)
        if errors:
            return None, errors

    return parsed, []


def _collect_schema_errors(parsed: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """收集全部 Schema 校验错误，格式化为「字段路径: message」的人话（问题 3）。

    一次收集全部错误（iter_errors 全量，非第一条），让模型一次改完。
    返回空列表 = 校验通过。
    """
    errors = []
    for e in Draft7Validator(schema).iter_errors(parsed):
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errors.append(f"- 字段 `{path}`：{e.message}")
    return errors


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
    except Exception as e:  # schema 本身非法（非标准关键字等）  # noqa: BLE001
        logger.error(
            "Schema 校验器异常（schema 可能非法）: %s",
            e,
        )
        return False


class StructuredOutput:
    """
    结构化输出提取器（三级降级实现载体）。

    优先使用 OpenAI 原生 JSON Schema（response_format），
    兜底使用 JSON Mode、prompt 约束 + 正则提取。
    统一对外入口：LLMService.generate_structured()（本类为内部实现）。
    """

    # 默认输出预算：extract 未传 max_tokens 时用（由 register_config 注入 settings 值）。
    _default_max_tokens: int = 2048

    @classmethod
    def register_config(cls, max_tokens: int) -> None:
        """注入默认输出预算（AppState 读 settings 后调用），替代模块内硬编码。"""
        cls._default_max_tokens = max_tokens

    @staticmethod
    async def extract(
        llm_service: Any,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
        max_tokens: int | None = None,
    ) -> dict[str, Any] | None:
        """
        根据 JSON Schema 从消息中提取结构化数据（三级降级）。

        问题 2 语义：截断（StructuredTruncationError）短路返回 None——
        不进入降级链（截断与降级正交，降级无益只会浪费调用）。
        拒答（StructuredRefusalError）向上抛——调用方需区分「三级耗尽」与「拒答」，
        拒答通常需要业务层差异化处理（安全兜底/文案）。

        Args:
            llm_service: LLMService 实例（generate 代理）
            messages: 完整消息列表（调用方构建）
            schema: JSON Schema 定义
            model_key: 使用的模型标识（默认 fast，低延迟低成本）
            max_tokens: 输出预算上限。None 用 register_config 注入的默认值
                （AppState 注入 settings.llm_structured_max_tokens，默认 2048）；
                截断时扩 2 倍重试 1 次。

        Returns:
            解析后的 dict，三级均失败返回 None

        Raises:
            StructuredRefusalError: 模型拒答（内容安全策略触发），不强行 repair
        """
        # 问题 4：递归补全 additionalProperties:false（深拷贝，不污染调用方 schema）。
        # 默认拒绝额外字段，模型无法扩展接口混入业务不需要的字段。
        schema = _enforce_no_extra_fields(schema)
        if max_tokens is None:
            max_tokens = StructuredOutput._default_max_tokens

        # 第一级：先用原生 JSON Schema
        response_format = _build_json_schema_request(schema)
        try:
            result = await StructuredOutput._try_extract(
                llm_service,
                messages,
                response_format,
                model_key,
                schema=schema,
                max_tokens=max_tokens,
            )
        except StructuredTruncationError:
            return None  # 截断短路，不降级
        if result is not None:
            return result

        # 第二级：降级普通 JSON mode
        response_format = _build_json_mode_request()
        try:
            result = await StructuredOutput._try_extract(
                llm_service,
                messages,
                response_format,
                model_key,
                schema=schema,
                max_tokens=max_tokens,
            )
        except StructuredTruncationError:
            return None  # 截断短路，不降级
        if result is not None:
            return result

        # 第三级：最终降级纯 prompt 约束 + 正则提取
        try:
            return await StructuredOutput._fallback_extract(
                llm_service,
                messages,
                model_key,
                schema=schema,
                max_tokens=max_tokens,
            )
        except StructuredTruncationError:
            return None  # 截断短路

    @staticmethod
    async def _try_extract(
        llm_service: Any,
        messages: list[dict],
        response_format: dict,
        model_key: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any] | None:
        """尝试用指定 response_format 提取（解析前做边界检查）。

        问题 2 语义：
        - 截断（length/max_tokens/insufficient_system_resource）：本层扩 max_tokens
          重试 1 次；重试后仍失败抛 StructuredTruncationError（短路，不降级）
        - 拒答（refusal/content_filter/content 空）：抛 StructuredRefusalError
          （短路，不强行 repair）
        - 工具调用（finish_reason=tool_calls，content 空是正常形态）：抛
          StructuredToolCallError（短路，不进回喂/降级循环——模型已放弃输出 JSON，
          JSON mode/纯 prompt 对工具调用无意义，反复降级浪费调用；交回调用方按
          工具调用处理）
        - 正常：解析 + Schema 校验，普通失败返回 None（触发降级）

        Args:
            schema: 传入则对解析结果做 Schema 校验（校验失败返回 None 触发降级）
        """
        result = await StructuredOutput._call_generate(
            llm_service,
            messages,
            model_key,
            max_tokens,
            response_format=response_format,
        )
        if result is None:
            return None

        failure = StructuredOutput._classify_result(result)

        if failure == "truncated":
            logger.warning(
                "结构化输出截断: finish_reason=%s, 扩 max_tokens 重试 1 次",
                result.finish_reason,
            )

            retry = await StructuredOutput._call_generate(
                llm_service,
                messages,
                model_key,
                max_tokens * 2
                if max_tokens is not None
                else StructuredOutput._default_max_tokens * 2,
                response_format=response_format,
                stage="结构化输出截断重试",
            )
            if retry is None:
                return None  # 下游失败 → 降级，与首次调用语义一致

            retry_failure = StructuredOutput._classify_result(retry)

            StructuredOutput._raise_boundary(retry_failure, retry, "截断重试后")
            result = retry
        else:
            # 未走截断重试分支：failure 反映当前 result，可安全短路 refusal/tool_calls
            StructuredOutput._raise_boundary(failure, result, "结构化输出")

        # 正常：解析 + 校验（错误回喂重试，问题 3）
        # 同一 response_format（同一级约束）下重试：把具体校验错误回喂模型修正。
        # 回喂耗尽 → 返回 None 触发降级（与现有降级链无缝衔接）。
        content = result.content
        for _ in range(_REASK_MAX_RETRIES):
            parsed, errors = _parse_and_validate(content, schema)
            if parsed is not None:
                return parsed

            logger.warning(
                "结构化输出解析/校验失败（回喂第 %d 次）: %s",
                _ + 1,
                "\n".join(errors),
            )
            # 回喂：clone + assistant 失败输出 + user 错误反馈（不污染调用方 messages）
            retry = await StructuredOutput._call_generate(
                llm_service,
                _build_reask_messages(messages, content, "\n".join(errors)),
                model_key,
                max_tokens,
                response_format=response_format,
                stage="结构化输出回喂",
            )
            if retry is None:
                return None  # 下游失败 → 降级

            failure = StructuredOutput._classify_result(retry)
            # 回喂循环内截断 → 一律短路（与顶层「截断与降级正交」一致），
            # 不与扩 token 逻辑组合，防 token 爆炸。
            StructuredOutput._raise_boundary(failure, retry, "回喂重试后")
            content = retry.content

        return None  # 回喂耗尽 → 降级

    @staticmethod
    async def _fallback_extract(
        llm_service: Any,
        messages: list[dict],
        model_key: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any] | None:
        """纯 prompt 约束降级方案（同样做三态检查，截断/拒答短路）。

        第三级到头了无降级可走：截断不扩 token 重试（纯 prompt 约束重试收益不定），
        拒答/截断记日志后抛异常短路。
        """
        result = await StructuredOutput._call_generate(
            llm_service,
            messages,
            model_key,
            max_tokens,
            stage="结构化输出 fallback",
        )
        if result is None:
            return None

        failure = StructuredOutput._classify_result(result)

        StructuredOutput._raise_boundary(failure, result, "结构化输出（fallback）")

        # 尝试提取 JSON 块（问题 5 补正则：模型可能在 JSON 前后加说明文字）
        content = result.content.strip()
        # 1) 移除 markdown 代码块围栏后整体解析
        fenced = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
        fenced = re.sub(r"\s*```$", "", fenced, flags=re.MULTILINE)
        parsed = _try_parse_json(fenced, schema)
        if parsed is not None:
            return parsed
        # 2) 正则定位首个 `{` 到末个 `}` 的候选块（prose 包裹场景）
        m = re.search(r"\{.*\}", fenced, flags=re.DOTALL)
        if m:
            parsed = _try_parse_json(m.group(0), schema)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    async def _call_generate(
        llm_service: Any,
        messages: list[dict],
        model_key: str,
        max_tokens: int | None,
        *,
        response_format: dict | None = None,
        stage: str = "结构化输出",
    ) -> Any | None:
        """调用 generate 并统一处理下游异常（_try_extract/_fallback_extract 复用）。

        不可恢复错误（4xx/认证/熔断，NON_RETRYABLE）向上抛——generate 已对
        NON_RETRYABLE raise，此处防御性兜底；可恢复错误（超时/5xx/429）可靠性层
        已重试耗尽，generate 转 None，此处同样返回 None 触发降级。
        """
        try:
            return await llm_service.generate(
                messages=messages,
                temperature=0,
                max_tokens=max_tokens,
                response_format=response_format,
                model_key=model_key,
            )
        except Exception as e:
            # 不可恢复错误（4xx/认证/熔断）向上抛（generate 已对 NON_RETRYABLE raise）；
            # 可恢复错误（超时/5xx/429）重试耗尽已由 generate 转 None，此处兜底防御。
            if classify_error(e) == ErrorCategory.NON_RETRYABLE:
                logger.error("%s 下游不可恢复错误: %s", stage, e)
                raise
            return None  # 下游失败（可靠性层已重试），降级

    @staticmethod
    def _classify_result(result: Any) -> str:
        """分类结构化响应失败类型（解析前 API 边界检查，问题 2）。

        检查顺序：refusal 字段 → finish_reason 拒答/过滤 → finish_reason 截断 →
        finish_reason 工具调用 → content 空。
        返回 "ok" / "truncated" / "refusal" / "tool_calls"。
        """
        if getattr(result, "refusal", None):
            return "refusal"
        fr = result.finish_reason
        if fr in _REFUSAL_REASONS:
            return "refusal"
        if fr in _TRUNCATED_REASONS:
            return "truncated"
        if fr == "tool_calls":
            # 模型决定调用工具而非输出文本：content 为空是正常形态，不是拒答。
            # 作为独立短路类别：提取方遇此抛 StructuredToolCallError（模型已放弃
            # 输出 JSON，进回喂/降级只会浪费调用），交回调用方按工具调用处理。
            return "tool_calls"
        if not result.content:
            # content 空 + 正常结束 = 拒答/空回（DeepSeek 无 refusal 字段形态）
            return "refusal"
        return "ok"

    @staticmethod
    def _raise_boundary(failure: str, result: Any, stage: str) -> None:
        """按失败类型短路抛异常（refusal / tool_calls / truncated，可选统一处理）。

        - refusal / tool_calls 在任意调用点语义一致：模型已放弃输出 JSON，短路
          抛异常交回调用方处理，不进入回喂/降级循环（JSON mode/纯 prompt 对它们
          无意义）。
        - truncated 为「可选短路」：短路语义的调用点（截断重试后/回喂循环内/
          fallback）统一走这里；**主调用点例外**——截断时需扩 max_tokens 重试
          而非短路，由调用方在调用前自行处理。
        """
        if failure == "refusal":
            logger.warning(
                "%s拒答: refusal=%r, finish_reason=%s",
                stage,
                getattr(result, "refusal", None),
                result.finish_reason,
            )
            raise StructuredRefusalError(
                f"{stage}拒答: finish_reason={result.finish_reason}"
            )
        if failure == "tool_calls":
            logger.warning(
                "%s转为工具调用: finish_reason=%s, content 为空",
                stage,
                result.finish_reason,
            )
            raise StructuredToolCallError(
                f"{stage}转为工具调用: finish_reason={result.finish_reason}"
            )
        if failure == "truncated":
            logger.warning(
                "%s截断: finish_reason=%s",
                stage,
                result.finish_reason,
            )
            raise StructuredTruncationError(
                f"{stage}截断: finish_reason={result.finish_reason}"
            )
