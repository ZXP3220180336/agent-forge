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

import asyncio
import json
import re
import time
from typing import Any

from jsonschema import SchemaError, ValidationError

from app.domain.ports.llm_gateway import LLMGateway, StreamResult
from app.platform.observability.logger import get_logger
from app.shared.exceptions import (
    LLMCancelledError,
    LLMDeadlineExceededError,
    StructuredRefusalError,
    StructuredToolCallError,
    StructuredTruncationError,
)
from app.shared.json_schema import create_schema_validator

from . import structured_codec as codec
from .errors import decide_downstream_error, is_unsupported_response_format_error

logger = get_logger("llm.structured")


# 截断/拒答的 finish_reason 判定集合（问题 2 三态检查）。
# DeepSeek 额外有 insufficient_system_resource（推理资源中断）；Anthropic 用 max_tokens。
_TRUNCATED_REASONS = frozenset(["length", "max_tokens", "insufficient_system_resource"])
_REFUSAL_REASONS = frozenset(["content_filter"])

# 错误回喂重试（问题 3）：每级校验失败回喂上限（额外尝试次数，最多 3 次请求）。
# 工业共识 2~3 次；首次修正成功率最高，之后陡降。
_REASK_MAX_RETRIES = 2


def _parse_and_validate(
    content: str,
    schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """接收纯解析结果，将校验器异常记录并转换为既有回喂错误。"""
    try:
        return codec.parse_and_validate(content, schema)
    except Exception as e:  # noqa: BLE001  非法 schema 保持原有失败出口。
        logger.error("Schema 校验器异常（schema 可能非法）: %s", e)
        return None, [f"- Schema 校验器异常（schema 可能非法）：{e}"]


def _collect_schema_error_summaries(
    parsed: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    """生成日志摘要，并在校验器异常时保留原有观测与错误文本。"""
    try:
        return codec.collect_schema_error_summaries(parsed, schema)
    except Exception as e:  # noqa: BLE001  非法 schema 保持原有失败出口。
        logger.error("Schema 校验器异常（schema 可能非法）: %s", e)
        return [f"- Schema 校验器异常（schema 可能非法）：{e}"]


def _try_parse_json(
    content: str,
    schema: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """解析 fallback 候选，由本模块负责校验失败的日志和出口。"""
    parsed = codec.parse_json_object(content)
    if parsed is None:
        return None
    if schema is not None and not _validate_schema(parsed, schema):
        return None
    return parsed


def _validate_schema(parsed: dict[str, Any], schema: dict[str, Any]) -> bool:
    """按 JSON Schema 校验解析结果。

    返回 False 时校验失败原因已记录日志，调用方应触发降级。
    这是「模型返回不能直接进业务」的本地校验一环——strict 只锁结构，
    minimum/maximum/pattern 等值约束与 refusal/截断绕过，都靠这层兜底。
    """
    try:
        codec.validate_schema(parsed, schema)
        return True
    except ValidationError as e:
        # 错误摘要用结构化字段（字段路径 + 校验器 + 约束值）而非 e.message——
        # e.message 会嵌入完整实例值（如 `'<超长值>' is too long`），是敏感数据
        # 泄露面；validator/validator_value 只含 schema 结构信息，无实例数据。
        path = "/".join(str(p) for p in e.absolute_path) or "<root>"
        logger.warning(
            "结构化输出 Schema 校验失败: 字段 `%s` 违反 `%s`=%s (schema=%s, parsed=%s)",
            path,
            e.validator,
            e.validator_value,
            json.dumps(schema, ensure_ascii=False),
            # 模型输出可能含业务敏感数据（Yield RCA 场景为良率/晶圆数据），
            # 只记截断前缀，不把完整 parsed 落盘到日志（泄露面收敛）。
            codec.truncate_json_for_log(parsed),
        )
        return False
    except Exception as e:  # schema 本身非法（非标准关键字等）  # noqa: BLE001
        logger.error(
            "Schema 校验器异常（schema 可能非法）: %s",
            e,
        )
        return False


def _accumulate_usage(target: dict | None, src: dict | None) -> None:
    """把一次成功调用的 usage 累加进目标 dict（成本计量需反映全程真实消耗）。

    与 reflection._merge_usage 同语义：逐 key 相加。嵌套明细（token details）无可累加
    语义，保留最新值。
    """
    if target is None or not src:
        return
    for key, value in src.items():
        if isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value
        else:
            target[key] = value


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
        """注入默认输出预算（Container 读 settings 后调用），替代模块内硬编码。"""
        cls._default_max_tokens = max_tokens

    @staticmethod
    async def extract(
        llm_service: LLMGateway,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
        max_tokens: int | None = None,
        usage: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        """
        根据 JSON Schema 从消息中提取结构化数据（三级降级）。

        问题 2 语义：截断（StructuredTruncationError）短路返回 None——
        不进入降级链（截断与降级正交，降级无益只会浪费调用）。
        拒答（StructuredRefusalError）向上抛——调用方需区分「三级耗尽」与「拒答」，
        拒答通常需要业务层差异化处理（安全兜底/文案）。

        Args:
            llm_service: LLM 网关（本路径只调用 generate）
            messages: 完整消息列表（调用方构建）
            schema: 本地 Draft 2020-12 定义；预检失败记 ERROR 并返回 None，不调用模型
            model_key: 使用的模型标识（默认 fast，低延迟低成本）
            max_tokens: 输出预算上限。None 用 register_config 注入的默认值
                （Container 注入 settings.llm_structured_max_tokens，默认 2048）；
                截断时扩 2 倍重试 1 次。
            usage: 可选，可变引用回填本次 extract 全程调用的 token 用量累计
                （含多级降级 / 截断重试 / 回喂的所有成功调用，供成本计量）。
            cancel_event: 业务取消信号；已置位则**不再发起后续子调用**，直接返回
                None（与降级耗尽同出口——调用方既有 None 降级路径保证终止后无新调用）。
            deadline: 绝对截止时刻（time.monotonic）；已过则同上拦截。由调用方现算
                start_time + max_execution_time，同一时间预算不逐级重计；None = 不限制。

        Returns:
            解析后的 dict，三级均失败返回 None

        Raises:
            StructuredRefusalError: 模型拒答（内容安全策略触发），不强行 repair
        """
        # LLM-044（修正 2）：整条降级链在**最外层**收敛执行终止——_extract_impl 内任一
        # 级 generate 抛的执行终止（shared LLMCancelledError/LLMDeadlineExceededError）
        # 不得被当作「当前格式失败」继续 JSON mode / 回喂 / 扩容等剩余降级级；统一在
        # 此 return None（与降级耗尽同出口），整条链只终止一次。
        try:
            return await StructuredOutput._extract_impl(
                llm_service=llm_service,
                messages=messages,
                schema=schema,
                model_key=model_key,
                max_tokens=max_tokens,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except LLMCancelledError, LLMDeadlineExceededError:
            return None

    @staticmethod
    async def _extract_impl(
        llm_service: LLMGateway,
        messages: list[dict],
        schema: dict[str, Any],
        model_key: str = "fast",
        max_tokens: int | None = None,
        usage: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        """extract 的三级降级主体（与 extract 同参，由 extract 包装终止收敛）。"""
        try:
            create_schema_validator(schema)
        except SchemaError as error:
            logger.error("Schema 定义无效，未调用模型: %s", error.message)
            return None
        # 问题 4：递归补全 additionalProperties:false（深拷贝，不污染调用方 schema）。
        # 默认拒绝额外字段，模型无法扩展接口混入业务不需要的字段。
        schema = codec.enforce_no_extra_fields(schema)
        if max_tokens is None:
            max_tokens = StructuredOutput._default_max_tokens

        # 第一级：先用原生 JSON Schema
        response_format = codec.build_json_schema_request(schema)
        try:
            result = await StructuredOutput._try_extract(
                llm_service=llm_service,
                messages=messages,
                response_format=response_format,
                model_key=model_key,
                schema=schema,
                max_tokens=max_tokens,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except StructuredTruncationError:
            return None  # 截断短路，不降级
        if result is not None:
            return result

        # 第二级：降级普通 JSON mode
        response_format = codec.build_json_mode_request()
        try:
            result = await StructuredOutput._try_extract(
                llm_service=llm_service,
                messages=messages,
                response_format=response_format,
                model_key=model_key,
                schema=schema,
                max_tokens=max_tokens,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except StructuredTruncationError:
            return None  # 截断短路，不降级
        if result is not None:
            return result

        # 第三级：最终降级纯 prompt 约束 + 正则提取
        try:
            return await StructuredOutput._fallback_extract(
                llm_service=llm_service,
                messages=messages,
                model_key=model_key,
                schema=schema,
                max_tokens=max_tokens,
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except StructuredTruncationError:
            return None  # 截断短路

    @staticmethod
    async def _try_extract(
        *,
        llm_service: LLMGateway,
        messages: list[dict],
        response_format: dict,
        model_key: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        usage: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
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
            llm_service=llm_service,
            messages=messages,
            model_key=model_key,
            max_tokens=max_tokens,
            response_format=response_format,
            usage=usage,
            cancel_event=cancel_event,
            deadline=deadline,
        )
        if result is None:
            return None

        failure = StructuredOutput._classify_result(result)

        if failure == "empty":
            # 适配层空响应 / 流中断无结果（无 refusal、无 finish_reason、content 空）→
            # 业务无结果，返回 None 触发降级（LLM-004）；不短路拒答、不进回喂
            # （空 content 回喂无意义，白打调用）。
            return None

        if failure == "truncated":
            logger.warning(
                "结构化输出截断: finish_reason=%s, 扩 max_tokens 重试 1 次",
                result.finish_reason,
            )

            retry = await StructuredOutput._call_generate(
                llm_service=llm_service,
                messages=messages,
                model_key=model_key,
                max_tokens=max_tokens * 2
                if max_tokens is not None
                else StructuredOutput._default_max_tokens * 2,
                response_format=response_format,
                stage="结构化输出截断重试",
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            if retry is None:
                return None  # 下游失败 → 降级，与首次调用语义一致

            retry_failure = StructuredOutput._classify_result(retry)

            if retry_failure == "empty":
                # 适配层空响应 / 流中断无结果（无 refusal、无 finish_reason、content 空）→
                # 业务无结果，返回 None 触发降级（LLM-004）；不短路拒答、不进回喂
                # （空 content 回喂无意义，白打调用）。
                return None

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
                # usage 已由每次成功调用在 _call_generate 累加，此处不再回填
                return parsed

            # 日志脱敏：schema 校验失败的错误文本（`- 字段 …：e.message`）含
            # `e.message` 嵌入完整实例值——改用结构化字段摘要（validator/约束值，
            # 无实例数据），防业务敏感数据落盘；解析失败/非 dict 的错误本身
            # 不含实例值（JSONDecodeError 只报位置），原样记录。
            if schema is not None and errors and errors[0].startswith("- 字段"):
                log_errors = _collect_schema_error_summaries(
                    json.loads(content),
                    schema,  # 走到此 content 必为可解析 dict
                )
            else:
                log_errors = errors

            logger.warning(
                "结构化输出解析/校验失败（回喂第 %d 次）: %s",
                _ + 1,
                "\n".join(log_errors),
            )
            # 回喂：clone + assistant 失败输出 + user 错误反馈（不污染调用方 messages）
            retry = await StructuredOutput._call_generate(
                llm_service=llm_service,
                messages=codec.build_reask_messages(
                    messages, content, "\n".join(errors)
                ),
                model_key=model_key,
                max_tokens=max_tokens,
                response_format=response_format,
                stage="结构化输出回喂",
                usage=usage,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            if retry is None:
                return None  # 下游失败 → 降级

            failure = StructuredOutput._classify_result(retry)

            if failure == "empty":
                # 适配层空响应 / 流中断无结果（无 refusal、无 finish_reason、content 空）→
                # 业务无结果，返回 None 触发降级（LLM-004）；不短路拒答、不进回喂
                # （空 content 回喂无意义，白打调用）。
                return None

            # 回喂循环内截断 → 一律短路（与顶层「截断与降级正交」一致），
            # 不与扩 token 逻辑组合，防 token 爆炸。
            StructuredOutput._raise_boundary(failure, retry, "回喂重试后")
            content = retry.content

        # 终态解析：最后一次回喂请求的输出尚未被解析——循环「解析→失败→再请求」
        # 以「请求」收尾，循环退出时 content 是最新一次回喂的输出。若不补这一次
        # 解析，模型在最后一次回喂修正成功的结果会被静默丢弃（返回 None + 白付一次
        # 调用）。循环退出后再解析一次，保证每次请求的输出都经过解析/校验。
        parsed, _ = _parse_and_validate(content, schema)
        return parsed  # 回喂耗尽（含终态）仍失败 → None 触发降级

    @staticmethod
    async def _fallback_extract(
        *,
        llm_service: LLMGateway,
        messages: list[dict],
        model_key: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        usage: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        """纯 prompt 约束降级方案（同样做三态检查，截断/拒答短路）。

        第三级到头了无降级可走：截断不扩 token 重试（纯 prompt 约束重试收益不定），
        拒答/截断记日志后抛异常短路。
        """
        result = await StructuredOutput._call_generate(
            llm_service=llm_service,
            messages=messages,
            model_key=model_key,
            max_tokens=max_tokens,
            stage="结构化输出 fallback",
            usage=usage,
            cancel_event=cancel_event,
            deadline=deadline,
        )
        if result is None:
            return None

        failure = StructuredOutput._classify_result(result)
        if failure == "empty":
            # 空响应无结果（LLM-004）：第三级已无降级可走，返回 None（业务无结果）
            return None

        StructuredOutput._raise_boundary(failure, result, "结构化输出（fallback）")

        # 尝试提取 JSON 块（问题 5 补正则：模型可能在 JSON 前后加说明文字）
        content = result.content.strip()
        # 1) 移除 markdown 代码块围栏后整体解析
        fenced = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
        fenced = re.sub(r"\s*```$", "", fenced, flags=re.MULTILINE)
        parsed = _try_parse_json(fenced, schema)
        if parsed is not None:
            return parsed  # usage 已由 _call_generate 累加
        # 2) 正则定位首个 `{` 到末个 `}` 的候选块（prose 包裹场景）
        m = re.search(r"\{.*\}", fenced, flags=re.DOTALL)
        if m:
            parsed = _try_parse_json(m.group(0), schema)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    async def _call_generate(
        *,
        llm_service: LLMGateway,
        messages: list[dict],
        model_key: str,
        max_tokens: int | None,
        response_format: dict | None = None,
        stage: str = "结构化输出",
        usage: dict | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> StreamResult | None:
        """调用 generate 并统一处理下游异常（_try_extract/_fallback_extract 复用）。

        不可恢复错误（4xx/认证/熔断，NON_RETRYABLE）向上抛——generate 已对
        NON_RETRYABLE raise，此处防御性兜底；
        可恢复错误（超时/5xx/429）可靠性层已重试耗尽，generate 转 None，
        此处同样返回 None 触发降级。

        所有真实成功调用（多级降级 / 截断重试 / 回喂任一）在此累加 usage——成本计量
        需要全程消耗，只回填"最后一次成功"会系统性低估（缺陷修复的单一归口）。

        cancel_event / deadline：每笔真实请求前的执行护栏。命中时抛 shared 类型化
        终止信号，由 extract 最外层一次性收敛为 None，整条降级链不再空转；信号同时
        透传给 generate，约束其 reserve、SDK create、retry 与 fallback。
        """
        # 入口命中必须抛到 extract 最外层统一收敛；若仅返回 None，_extract_impl 会继续
        # 访问 JSON mode / prompt fallback，虽然不发 SDK 请求，仍会空转整条降级链。
        if cancel_event is not None and cancel_event.is_set():
            raise LLMCancelledError("用户取消")
        if deadline is not None and time.monotonic() >= deadline:
            raise LLMDeadlineExceededError("执行期限耗尽")
        try:
            # max_tokens 上游（extract）已把 None 归一为默认预算，此处兜底防御直接调用
            result = await llm_service.generate(
                messages=messages,
                temperature=0,
                max_tokens=(
                    max_tokens
                    if max_tokens is not None
                    else StructuredOutput._default_max_tokens
                ),
                response_format=response_format,
                model_key=model_key,
                # LLM-044：把执行控制信号透传到每次真实 SDK attempt（reserve/create/
                # 返回前检查点按同一 cancel_event + 绝对 deadline 受控）——不是只在
                # 本入口检查一次后放任 generate 内部 retry/fallback 在终止后继续。
                cancel_event=cancel_event,
                deadline=deadline,
            )
        except Exception as e:
            # LLM-044：执行控制终止（generate Facade 翻译后的 shared 领域异常）——
            # 累计本次异常携带的 usage 后**继续 raise**，由 extract 最外层收敛为
            # return None：整条结构化降级链只终止一次，不空转 JSON mode / 回喂 /
            # 扩容等剩余降级级（D1 修正）。
            if isinstance(e, (LLMCancelledError, LLMDeadlineExceededError)):
                if usage is not None and getattr(e, "usage", None):
                    _accumulate_usage(usage, e.usage)
                raise

            # 整体 deadline 防御（todo §4）：外层策略绝对截止（asyncio.timeout 到期）
            # 注入的**内置 TimeoutError** 是执行终止信号——直接 re-raise 保留终止语义，
            # 不得被 decide_downstream_error 归为 RETRYABLE 降级再调用。
            # openai APITimeoutError / httpx.TimeoutException 均非内置 TimeoutError，
            # 仍走下方可恢复路径，不误伤网络超时语义。
            if isinstance(e, TimeoutError):
                raise

            # 明确因「response_format 不被支持」而 400（模型/兼容网关不支持
            # strict json_schema）：这不是调用方 bug，而是约束模式不被支持——
            # 降级到下一级（JSON mode / 正则）而非致命上抛，兑现降级链契约。
            if is_unsupported_response_format_error(e):
                logger.warning(
                    "%s 模型/网关不支持 response_format，降级到下一级: %s",
                    stage,
                    e,
                )
                return None

            # 统一决策（llm/errors.py）：可恢复错误（超时/5xx/429）兜底防御降级；
            # 不可恢复错误（generate 已归一为 LLMAPIError 或原样）上抛交上层决策。
            decision = decide_downstream_error(e)
            if decision.to_raise is None:
                return None
            logger.error("%s 下游不可恢复错误: %s", stage, e)
            if decision.normalized:
                raise decision.to_raise from e
            raise

        # usage 累加：每次真实成功调用（多级降级 / 截断重试 / 回喂任一）的 token 消耗
        # 都计入成本计量——只回填"最后一次成功"会低估真实开销。
        if result is not None:
            _accumulate_usage(usage, result.usage)
        return result

    @staticmethod
    def _classify_result(result: StreamResult) -> str:
        """分类结构化响应失败类型（解析前 API 边界检查，问题 2）。

        检查顺序：refusal 字段 → finish_reason 拒答/过滤 → finish_reason 截断 →
        finish_reason 工具调用 → content 空。
        返回 "ok" / "truncated" / "refusal" / "tool_calls" / "empty"。
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
            if not result.finish_reason:
                # 无 refusal、无 finish_reason、content 空 = 适配层空响应 / 流中断无结果，
                # 非拒答/截断/工具调用（LLM-004）→ 独立分类 empty → 触发降级（返回 None），
                # 不短路为拒答（空 content 不是可靠拒答信号，工业界按生成失败处理）。
                return "empty"
            # content 空 + 有 finish_reason（如 stop）= DeepSeek 拒答形态（无 refusal 字段）
            return "refusal"
        return "ok"

    @staticmethod
    def _raise_boundary(failure: str, result: StreamResult, stage: str) -> None:
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
                "%s拒答: refusal=%s, finish_reason=%s",
                stage,
                # LLM-008：拒答文本经截断落盘（「模型输出不完整落盘」安全基线）——
                # 拒答常引用触发内容（Yield RCA 晶圆/良率数据），不能完整落日志。
                # 异常 message 保持简洁（不含拒答文本），日志保留截断前缀供诊断。
                codec.truncate_text_for_log(str(getattr(result, "refusal", "") or "")),
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
