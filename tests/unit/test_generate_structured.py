"""
LLMService.generate_structured 单元测试

覆盖「统一结构化输出入口」决策（generate_structured 委托 StructuredOutput 三级降级）：
    第一级：原生 JSON Schema（strict=True）
    第二级：JSON Mode（json_object）
    第三级：纯 prompt 约束 + 正则提取

不依赖真实 API：mock LLMService.generate（async），构造 StreamResult 返回。
通过真实委托验证 generate_structured 内部走三级降级（断言各级 generate 调用参数）。
"""

import asyncio
import copy
import json
import logging
import time

import httpx
import pytest

from app.integration.llm.llm_service import LLMService
from app.domain.ports.llm_gateway import StreamResult
from app.integration.llm.structured import (
    StructuredOutput,
    StructuredRefusalError,
    StructuredTruncationError,
)
from app.integration.llm.structured_codec import enforce_no_extra_fields
from app.shared.exceptions import LLMCancelledError, LLMDeadlineExceededError

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}
MESSAGES = [{"role": "user", "content": "张三去了北京"}]


async def test_nested_strict_schema_preserves_local_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    """strict 请求副本收紧嵌套字段，本地校验和调用方仍保留显式扩展许可。"""
    llm = LLMService()
    schema = {
        "type": "object",
        "properties": {
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"source": {"type": "string"}},
                    "required": ["source"],
                    "additionalProperties": True,
                },
            },
        },
        "required": ["evidence"],
    }
    original = copy.deepcopy(schema)
    expected = {"evidence": [{"source": "FDC", "measurement": 82}]}
    calls: list[dict] = []

    async def fake_generate(**kwargs: object) -> StreamResult:
        """记录真实组装参数并返回带合法扩展字段的模型结果。"""
        calls.append(kwargs)
        return _sr(json.dumps(expected))

    monkeypatch.setattr(llm, "generate", fake_generate)
    assert await llm.generate_structured(MESSAGES, schema) == expected
    assert len(calls) == 1
    request_schema = calls[0]["response_format"]["json_schema"]["schema"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["evidence"]["items"]["additionalProperties"] is False
    assert schema == original


async def test_reask_preserves_all_errors_without_logging_instances(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """完整错误和失败原文送回模型，日志只含摘要且调用方消息不变。"""
    llm = LLMService()
    schema = {
        "type": "object",
        "properties": {"source": {"type": "integer"}, "confidence": {"type": "number"}},
        "required": ["source", "confidence"],
    }
    messages = [{"role": "user", "content": "请核对证据"}]
    original = copy.deepcopy(messages)
    raw = json.dumps({"source": "private-wafer-id", "confidence": "private-yield-value"})
    expected = {"source": 1, "confidence": 0.8}
    calls: list[dict] = []

    async def fake_generate(**kwargs: object) -> StreamResult:
        """首次返回两个非法字段，收到回喂后给出合法结果。"""
        calls.append(kwargs)
        return _sr(raw if len(calls) == 1 else json.dumps(expected))

    monkeypatch.setattr(llm, "generate", fake_generate)
    with caplog.at_level(logging.WARNING, logger="app.llm.structured"):
        assert await llm.generate_structured(messages, schema) == expected
    assert len(calls) == 2
    assert calls[0]["response_format"] == calls[1]["response_format"]
    feedback = calls[1]["messages"]
    assert feedback[:-2] == original
    assert feedback[-2] == {"role": "assistant", "content": raw}
    assert feedback[-1]["role"] == "user"
    for value in ("source", "confidence", "private-wafer-id", "private-yield-value"):
        assert value in feedback[-1]["content"]
    assert "source" in caplog.text and "confidence" in caplog.text
    assert "private-wafer-id" not in caplog.text
    assert "private-yield-value" not in caplog.text
    assert messages == original


def _sr(
    content: str | None,
    finish_reason: str | None = None,
    refusal: str | None = None,
) -> StreamResult:
    """构造带指定 content / finish_reason / refusal 的 StreamResult。"""
    sr = StreamResult()
    sr.content = content or ""
    sr.finish_reason = finish_reason
    sr.refusal = refusal
    return sr


def _sr_none() -> None:
    """模拟 generate 返回 None（调用失败）。"""
    return None


# =====================================================================
# 三级降级：每级成功路径
# =====================================================================


@pytest.mark.asyncio
async def test_first_level_schema_success():
    """第一级成功：合法 dict 直接返回，且 response_format 为 json_schema。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        seen["response_format"] = response_format
        seen["model_key"] = model_key
        seen["max_tokens"] = max_tokens
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "张三"}
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert seen["model_key"] == "fast"
    assert seen["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_second_level_json_mode_fallback():
    """第一级回喂耗尽失败 → 第二级 json_object 成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) <= 3:  # 第一级 + 回喂 2 次（均解析失败）
            return _sr("not valid json {")
        return _sr(json.dumps({"name": "李四"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "李四"}
    # 第一级(1) + 回喂(2) 均 json_schema，耗尽后降级第二级(1) json_object
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_schema"
    assert calls[2]["type"] == "json_schema"
    assert calls[3]["type"] == "json_object"
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_third_level_regex_fallback():
    """前两级（含回喂）失败 → 第三级正则 fallback 成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) <= 2:  # 第一级 + 回喂 2 次
            return _sr("bad json")
        if len(calls) <= 5:  # 第二级 + 回喂 2 次
            return _sr("bad json")
        return _sr("```json\n{\"name\": \"王五\"}\n```")  # 无 response_format，带代码块

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "王五"}
    # 第一级(1)+回喂(2) json_schema → 第二级(1)+回喂(2) json_object → 第三级(1) 无 response_format
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_schema"
    assert calls[2]["type"] == "json_schema"
    assert calls[3]["type"] == "json_object"
    assert calls[4]["type"] == "json_object"
    assert calls[5]["type"] == "json_object"
    assert calls[6] is None
    assert len(calls) == 7


@pytest.mark.asyncio
async def test_usage_accumulates_across_degrade_and_reask():
    """usage 回填累计全程真实消耗（降级 + 回喂的成功调用都计入），非最后一次成功。

    回归护栏：修复前 usage 只回填最后一次成功调用、且回喂路径取错变量（result 而非
    retry 的 usage）——成本护栏（reflection._merge_usage 累计）据此系统性低估。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        sr = _sr(
            "not valid json {" if len(calls) <= 3
            else json.dumps({"name": "李四"}, ensure_ascii=False)
        )
        n = len(calls)
        sr.usage = {
            "prompt_tokens": 10 * n,
            "completion_tokens": n,
            "total_tokens": 11 * n,
        }
        return sr

    llm.generate = fake_generate
    usage: dict = {}
    result = await llm.generate_structured(MESSAGES, SCHEMA, usage=usage)
    assert result == {"name": "李四"}
    assert len(calls) == 4  # 第一级(1) + 回喂(2) 均失败 → 第二级(1) 成功
    # 4 次成功调用全部累加：10+20+30+40 / 1+2+3+4 / 11+22+33+44
    assert usage == {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "total_tokens": 110,
    }


# =====================================================================
# 失败路径
# =====================================================================


@pytest.mark.asyncio
async def test_json_parse_failure_returns_none():
    """三级均返回非法 JSON → None。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        return _sr("definitely not json")

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None


@pytest.mark.asyncio
async def test_non_dict_content_returns_none():
    """content 是 JSON 数组（非 dict）→ None（isinstance dict 校验）。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        return _sr("[1, 2, 3]")

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None


@pytest.mark.asyncio
async def test_empty_response_returns_none():
    """generate 返回 None / 空 content → None。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        return _sr_none()

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None


@pytest.mark.asyncio
async def test_generate_unrecoverable_exception_propagates():
    """generate 抛不可恢复异常（未知=RuntimeError，B3）→ 向上抛，不静默降级。

    B3 契约变更前：generate 的 except Exception 一律吞掉返回 None，structured 白打
    降级请求。变更后：不可恢复错误（4xx/认证/熔断/未知异常归 NON_RETRYABLE）由
    generate raise，structured 记录 ERROR 日志后 re-raise——调用方感知真实失败。
    """
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        raise RuntimeError("downstream failure")

    llm.generate = fake_generate
    with pytest.raises(RuntimeError):
        await llm.generate_structured(MESSAGES, SCHEMA)


@pytest.mark.asyncio
async def test_generate_recoverable_exception_returns_none():
    """generate 抛可恢复异常（传输超时）→ 仍返回 None 降级（B3 保持降级契约）。

    网络可恢复超时用 httpx.TimeoutException 族代表（classify_error → RETRYABLE）；
    **内置 TimeoutError 已保留为「整体执行期限终止」语义**（E：直抛不降级，见
    test_builtin_timeout_reraises_not_degrade_to_next_level）——不再冒充网络超时。
    """
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        raise httpx.ReadTimeout("downstream timeout")

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None


# =====================================================================
# 参数透传与降级语义
# =====================================================================


@pytest.mark.asyncio
async def test_messages_passed_through():
    """generate 收到的 messages 就是调用方传入的完整 messages（不再内部拼接）。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        seen["messages"] = messages
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    await llm.generate_structured(MESSAGES, SCHEMA)
    assert seen["messages"] == MESSAGES


@pytest.mark.asyncio
async def test_schema_embedded_in_first_level():
    """第一级的 json_schema 内含补全后的 schema（additionalProperties:false）。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        seen["response_format"] = response_format
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    await llm.generate_structured(MESSAGES, SCHEMA)
    embedded = seen["response_format"]["json_schema"]["schema"]
    assert embedded["type"] == "object"
    assert embedded["properties"] == SCHEMA["properties"]
    assert embedded["required"] == SCHEMA["required"]
    assert embedded["additionalProperties"] is False  # 问题 4 补全
    assert seen["response_format"]["json_schema"]["name"] == "structured_output"


@pytest.mark.asyncio
async def test_model_key_forwarded():
    """model_key 透传到各级 generate（非默认值）。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        seen["model_key"] = model_key
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    await llm.generate_structured(MESSAGES, SCHEMA, model_key="reasoning")
    assert seen["model_key"] == "reasoning"


# =====================================================================
# Schema 校验（问题 1 修复：解析后按 schema 校验）
# 结构合法但不合 schema（类型/枚举/范围/必填）→ 校验失败 → 降级 / None
# =====================================================================

SCHEMA_RANGE = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["name", "confidence"],
}


@pytest.mark.asyncio
async def test_schema_validation_pass_no_extra_call():
    """第一级直接返回符合 schema → 校验通过、不降级。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr(json.dumps({"name": "张三", "confidence": 0.5}))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "张三", "confidence": 0.5}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_schema_validation_range_failure_falls_back():
    """第一级结构合法但 confidence 超范围（strict 不保证值约束）→ 回喂修正 → 成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr(json.dumps({"name": "张三", "confidence": 5}))
        return _sr(json.dumps({"name": "张三", "confidence": 0.9}))  # 回喂修正后成功

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "张三", "confidence": 0.9}
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_schema"  # 回喂保持同一级约束


@pytest.mark.asyncio
async def test_schema_validation_missing_required_falls_back():
    """第一级缺必填字段 confidence → 回喂修正 → 成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr(json.dumps({"name": "张三"}))
        return _sr(json.dumps({"name": "张三", "confidence": 0.8}))  # 回喂修正后成功

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "张三", "confidence": 0.8}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_schema_validation_all_levels_fail_returns_none():
    """三级均返回结构合法但不合 schema（含各级回喂）→ 全部校验失败 → None（含第三级正则路径也校验）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr(json.dumps({"name": "张三", "confidence": "很高"}))

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA_RANGE) is None
    # 第一级(1)+回喂(2) + 第二级(1)+回喂(2) + 第三级(1) = 7
    assert len(calls) == 7


# =====================================================================
# 问题 2：API 边界检查（finish_reason / refusal）
# 截断 → 本层扩 token 重试 1 次；拒答 → 短路不降级；正常 → 解析
# =====================================================================


@pytest.mark.asyncio
async def test_truncation_retries_with_larger_max_tokens():
    """第一级截断（length）→ 本层扩 max_tokens 重试 1 次 → 成功。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        if not seen.get("first"):
            seen["first"] = True
            return _sr('{"name": "张', finish_reason="length")  # 截断的半 JSON
        seen["retry_max_tokens"] = max_tokens
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "张三"}
    assert seen["retry_max_tokens"] == 4096  # 扩 token 重试


@pytest.mark.asyncio
async def test_max_tokens_param_overrides_settings():
    """调用方传入 max_tokens 覆盖 settings 默认（W4）。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        seen["max_tokens"] = max_tokens
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA, max_tokens=8192)
    assert result == {"name": "张三"}
    assert seen["max_tokens"] == 8192, "调用方 max_tokens 应覆盖 settings 默认"


@pytest.mark.asyncio
async def test_max_tokens_truncation_retry_doubles_param():
    """截断重试的 max_tokens 随调用方参数 ×2 缩放（W4），非硬编码 4096。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        if not seen.get("first"):
            seen["first"] = True
            return _sr('{"name": "张', finish_reason="length")
        seen["retry_max_tokens"] = max_tokens
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA, max_tokens=8192)
    assert result == {"name": "张三"}
    assert seen["retry_max_tokens"] == 8192 * 2, "截断重试应按调用方 max_tokens ×2"
    assert seen["retry_max_tokens"] != 4096, "不再硬编码 4096"


@pytest.mark.asyncio
async def test_truncation_retry_still_truncated_returns_none():
    """第一级截断 → 扩 token 重试仍截断 → 短路返回 None（不降级）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(max_tokens)
        return _sr('{"name": "张', finish_reason="length")

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None
    assert calls == [2048, 4096]  # 只本层重试 1 次，不再走降级链


@pytest.mark.asyncio
async def test_refusal_short_circuits_no_retry():
    """拒答（refusal 字段）→ 抛 StructuredRefusalError，不 repair、不降级，只调用一次。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr("", refusal="抱歉，我无法处理这个请求。")

    llm.generate = fake_generate
    with pytest.raises(StructuredRefusalError):
        await llm.generate_structured(MESSAGES, SCHEMA)
    assert len(calls) == 1  # 拒答短路，无降级重试


@pytest.mark.asyncio
async def test_content_filter_short_circuits():
    """finish_reason=content_filter → 抛 StructuredRefusalError，不 repair、不降级。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr("", finish_reason="content_filter")

    llm.generate = fake_generate
    with pytest.raises(StructuredRefusalError):
        await llm.generate_structured(MESSAGES, SCHEMA)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_refusal_from_third_level_short_circuits():
    """第三级 fallback 也做拒答短路（抛 StructuredRefusalError，第三级无降级可走）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) <= 2:  # 第一级 + 回喂 2 次
            return _sr("bad json")
        if len(calls) <= 5:  # 第二级 + 回喂 2 次
            return _sr("bad json")
        return _sr("", refusal="无法提供结构化数据")

    llm.generate = fake_generate
    with pytest.raises(StructuredRefusalError):
        await llm.generate_structured(MESSAGES, SCHEMA)
    # 第一级(1)+回喂(2) + 第二级(1)+回喂(2) + 第三级(1 拒答短路) = 6
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_fallback_truncation_short_circuits():
    """第三级 fallback 截断 → 抛 StructuredTruncationError（不扩 token 重试，无降级可走）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) <= 2:  # 第一级 + 回喂 2 次
            return _sr("bad json")
        if len(calls) <= 5:  # 第二级 + 回喂 2 次
            return _sr("bad json")
        return _sr('{"name": "张', finish_reason="length")

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None
    # 第一级(1)+回喂(2) + 第二级(1)+回喂(2) + 第二级第3次(截断短路) = 6
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_empty_content_normal_finish_treated_as_refusal():
    """content 空 + finish_reason=stop（DeepSeek 无 refusal 字段形态）→ 当拒答短路抛异常。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr("", finish_reason="stop")

    llm.generate = fake_generate
    with pytest.raises(StructuredRefusalError):
        await llm.generate_structured(MESSAGES, SCHEMA)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_empty_content_no_finish_treated_as_no_result():
    """content 空 + 无 finish_reason（适配层空响应）→ 返回 None 触发降级，不抛拒答（LLM-004）。

    修复前：`_classify_result` 把一切 content 空归 refusal，适配层空响应（无 refusal、
    无 finish_reason）被误判为安全拒答抛 StructuredRefusalError。
    修复后：无 refusal、无 finish_reason、content 空 → "empty" → 返回 None（业务无结果，
    触发降级）；DeepSeek 拒答（finish_reason=stop + 空 content）仍短路拒答（上一用例）。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr("", finish_reason=None)  # 适配层空响应形态（parse_non_stream 空 choices）

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result is None, "空响应应返回 None（触发降级耗尽），而非抛 StructuredRefusalError"
    assert len(calls) == 3, "三级均空响应 → 三级降级各调 1 次"


@pytest.mark.asyncio
async def test_tool_calls_finish_not_treated_as_refusal():
    """finish_reason=tool_calls + content 空 → 短路抛 StructuredToolCallError。

    修复前（原始）：`_classify_result` 的 `if not result.content:` 未排除 tool_calls，
    工具调用响应（空 content 是正常形态）被误判为拒答抛 StructuredRefusalError。
    修复后：tool_calls 作为独立短路类别抛 StructuredToolCallError——模型已放弃输出
    JSON，降级到更宽松约束（JSON mode/纯 prompt）无意义，短路不进入降级链，
    交回调用方按工具调用处理。
    """
    from app.integration.llm.structured import StructuredToolCallError

    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr("", finish_reason="tool_calls")

    llm.generate = fake_generate
    with pytest.raises(StructuredToolCallError):
        await llm.generate_structured(MESSAGES, SCHEMA)
    # 第一级 tool_calls 短路：只调用 1 次，不进降级链、不进回喂
    assert len(calls) == 1, "tool_calls 应短路抛异常，不进入降级链"


@pytest.mark.asyncio
async def test_normal_path_unaffected_by_classification():
    """正常响应（stop + 完整 JSON）→ 校验通过直接返回，不误判。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False), finish_reason="stop")

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "张三"}


# =====================================================================
# 问题 3：错误感知重试（校验失败回喂模型修正）
# 回喂重试成功 / 回喂耗尽降级 / 不污染 messages / 截断不进回喂循环
# =====================================================================


@pytest.mark.asyncio
async def test_reask_retries_then_success():
    """第一级校验失败（confidence 超范围）→ 回喂错误重试 → 成功。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        if not seen.get("first"):
            seen["first"] = True
            seen["first_messages"] = messages
            return _sr(json.dumps({"name": "张三", "confidence": 5}))  # 超范围
        seen["reask_messages"] = messages
        return _sr(json.dumps({"name": "张三", "confidence": 0.9}))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "张三", "confidence": 0.9}
    # 回喂消息 = 原 messages clone + assistant 失败输出 + user 错误反馈
    assert len(seen["reask_messages"]) == len(seen["first_messages"]) + 2
    assert seen["reask_messages"][-1]["role"] == "user"
    assert "Schema 校验" in seen["reask_messages"][-1]["content"]
    assert seen["reask_messages"][-2]["role"] == "assistant"
    assert seen["reask_messages"][-2]["content"] == json.dumps({"name": "张三", "confidence": 5})


@pytest.mark.asyncio
async def test_reask_succeeds_on_last_attempt():
    """回喂重试边界：最后一次（第 3 次）请求输出成功 → 正确返回。

    修复前：循环「解析→失败→再请求」共 3 次请求，但循环退出时第 3 次请求的
    输出从未被解析，直接 return None——模型在最后一次回喂修正成功时结果被静默
    丢弃（返回 None + 降级到更弱一级 + 白付一次调用）。
    修复后：循环退出补一次终态解析，最后一次回喂成功的结果被正确返回。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        # 第 1 次：超范围；回喂 1：仍超范围；回喂 2（最后一次）：修正成功
        if len(calls) < 3:
            return _sr(json.dumps({"name": "张三", "confidence": 5}))
        return _sr(json.dumps({"name": "张三", "confidence": 0.9}))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "张三", "confidence": 0.9}, (
        f"最后一次回喂成功的结果应返回，实际 {result!r}"
    )
    assert len(calls) == 3, "第一级 + 回喂 2 次 = 3 次请求"
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_schema"
    assert calls[2]["type"] == "json_schema"  # 回喂保持同一级约束


@pytest.mark.asyncio
async def test_reask_exhausted_falls_back():
    """第一级回喂耗尽（2 次）仍校验失败 → 降级到第二级成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) <= 3:  # 第一级 + 回喂 2 次，均校验失败
            return _sr(json.dumps({"name": "张三", "confidence": 5}))
        return _sr(json.dumps({"name": "李四", "confidence": 0.8}))  # 第二级成功

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "李四", "confidence": 0.8}
    assert len(calls) == 4  # 第一级(1) + 回喂(2) + 第二级(1)
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_schema"  # 回喂保持同一级约束
    assert calls[2]["type"] == "json_schema"
    assert calls[3]["type"] == "json_object"  # 耗尽后降级


@pytest.mark.asyncio
async def test_reask_does_not_pollute_caller_messages():
    """回喂不污染调用方 messages（clone 而非就地 append）。"""
    llm = LLMService()
    seen = {}
    original_messages = [{"role": "user", "content": "张三去了北京"}]

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        if not seen.get("first"):
            seen["first"] = True
            seen["caller_messages_at_first"] = messages
            return _sr(json.dumps({"name": "张三", "confidence": 5}))
        seen["caller_messages_at_reask"] = messages
        return _sr(json.dumps({"name": "张三", "confidence": 0.9}))

    llm.generate = fake_generate
    await llm.generate_structured(original_messages, SCHEMA_RANGE)
    # 调用方原始 messages 未被就地修改
    assert original_messages == [{"role": "user", "content": "张三去了北京"}]
    # 第一次调用收到的 messages 就是原始列表（未污染）
    assert seen["caller_messages_at_first"] == original_messages


class _UnsupportedResponseFormat400(Exception):
    """模拟模型/网关不支持 response_format 的 400 错误（openai.BadRequestError 形态）。

    status_code=400 → classify_error 判 NON_RETRYABLE；错误信息含 response_format，
    供 _call_generate 识别「明确因 response_format 不被支持而 400」。
    """

    status_code = 400


class _GenericBadRequest400(Exception):
    """模拟非 response_format 的普通 400 错误（应仍上抛，不降级）。"""

    status_code = 400


@pytest.mark.asyncio
async def test_json_schema_unsupported_400_degrades_to_json_mode():
    """模型不支持 json_schema response_format（400）→ 降级到第二级 JSON Mode。

    修复前：_build_json_schema_request 无条件发 strict json_schema，模型/网关
    不支持时 API 返回 400 → classify_error 判 NON_RETRYABLE → generate re-raise
    → _call_generate re-raise → extract 只捕获截断异常 → 文档承诺的「不支持时
    降级到 JSON Mode」永远走不到，调用方收到裸 API 错误。
    修复后：_call_generate 识别「response_format 不被支持而 400」→ 返回 None
    触发降级到下一级。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) == 1:
            raise _UnsupportedResponseFormat400(
                "Unsupported response_format: json_schema"
            )
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "张三"}, (
        f"不支持 json_schema 应降级到 JSON Mode 成功，实际 {result!r}"
    )
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_object"  # 降级到第二级
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_generic_bad_request_400_still_propagates():
    """非 response_format 的 400（消息格式错）→ 仍上抛，不降级。

    保护：识别仅针对「明确因 response_format 不被支持」的 400，
    其余 NON_RETRYABLE 400（调用方 bug）仍向上抛，不静默吞掉。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        raise _GenericBadRequest400("Invalid messages format")

    llm.generate = fake_generate
    with pytest.raises(_GenericBadRequest400):
        await llm.generate_structured(MESSAGES, SCHEMA)
    assert len(calls) == 1, "普通 400 应上抛，不触发降级"


@pytest.mark.asyncio
async def test_reask_truncation_does_not_enter_loop():
    """回喂循环内截断 → 不进入回喂循环、不扩 token 组合 → 降级。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr(json.dumps({"name": "张三", "confidence": 5}))  # 校验失败 → 回喂
        if len(calls) == 2:
            return _sr('{"name": "张', finish_reason="length")  # 回喂后截断
        return _sr(json.dumps({"name": "李四", "confidence": 0.8}))  # 降级第二级成功

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA_RANGE) is None
    # 第一次(校验失败) + 回喂1次(截断→一律短路返回 None，不降级)
    assert len(calls) == 2
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_reask_empty_response_returns_none():
    """回喂响应为空响应（无 finish_reason + content 空）→ 返回 None 触发降级（LLM-004）。

    修复前（LLM-004 补充）：回喂循环未处理 empty 分类，空响应进回喂白打调用；
    且变量名笔误 `retry_failure` 在正常路径（未走截断分支）未定义抛 NameError。
    修复后：回喂空响应 → "empty" → 返回 None 降级，不抛异常。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr("bad json")  # 首次：无效 JSON → 进回喂
        return _sr("", finish_reason=None)  # 回喂：适配层空响应

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result is None, "回喂空响应应返回 None（降级），不抛 NameError"
    # 一级首次(无效 JSON) + 一级回喂(空) + 二级(空) + 三级 fallback(空) = 4 次调用
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_invalid_schema_returns_none_not_crash():
    """非法 schema（UnknownType / TypeError / SchemaError）→ 返回 None 不崩溃（LLM-007）。

    修复前：`_collect_schema_errors` 的 `Draft7Validator(schema).iter_errors` 对非法
    schema 抛异常穿透崩溃；`_validate_schema` 却有 except 兜底（两套路径不一致）。
    修复后：捕获记日志 + 返回错误 → 按校验失败处理触发降级 → 最终 None。
    """
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        return _sr('{"name": "张三"}')

    llm.generate = fake_generate

    invalid_schemas = [
        {"type": "object", "properties": 5},  # properties 非法类型
        {"type": 123},  # type 非法
        {"type": "nonexistent_type"},  # 未知 type → UnknownType
    ]
    for schema in invalid_schemas:
        result = await llm.generate_structured(MESSAGES, schema)
        assert result is None, f"非法 schema 应返回 None（触发降级），而非崩溃: {schema}"


@pytest.mark.asyncio
async def test_refusal_log_truncated(caplog):
    """拒答文本落日志截断（LLM-008）：超长 refusal 不完整落盘。

    修复前：`_raise_boundary` 日志 `refusal=%r` 完整落盘，违反「模型输出不完整
    落盘」安全基线（拒答常引用触发内容，Yield RCA 含晶圆/良率数据）。
    修复后：拒答文本经 `_truncate_text_for_log` 截断，日志含截断标记。
    """
    llm = LLMService()
    long_refusal = "敏感数据" * 400  # 1600 字符，超过 _LOG_TRUNCATE_LIMIT=500

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        return _sr("", refusal=long_refusal)

    llm.generate = fake_generate
    with caplog.at_level(logging.WARNING):
        with pytest.raises(StructuredRefusalError):
            await llm.generate_structured(MESSAGES, SCHEMA)

    assert "已截断" in caplog.text, "超长拒答应被截断标记"
    assert long_refusal not in caplog.text, "完整拒答文本不应落盘"


@pytest.mark.asyncio
async def test_strict_schema_normalizes_additional_properties_true():
    """strict 请求把 additionalProperties: true 归一为 false（LLM-009）。

    修复前：strict 固定 true，显式 additionalProperties: true 必然 400 且被
    `is_unsupported_response_format_error` 误判「模型不支持」白打调用。
    修复后：strict 请求 schema 递归归一 true→false（副本），本地校验仍用原 schema。
    """
    llm = LLMService()
    calls = []

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": True,  # strict 不支持
    }

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr('{"name": "张三"}')

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, schema)
    assert result == {"name": "张三"}
    assert calls[0]["type"] == "json_schema"
    req_schema = calls[0]["json_schema"]["schema"]
    assert req_schema["additionalProperties"] is False, (
        "strict 请求应把 additionalProperties: true 归一为 false"
    )
    assert schema["additionalProperties"] is True, "调用方 schema 不应被污染"


@pytest.mark.asyncio
async def test_reask_refusal_short_circuits():
    """回喂循环内拒答 → 抛 StructuredRefusalError（不降级、不继续回喂）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr(json.dumps({"name": "张三", "confidence": 5}))
        return _sr("", refusal="无法修正")

    llm.generate = fake_generate
    with pytest.raises(StructuredRefusalError):
        await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert len(calls) == 2  # 拒答短路，不降级不继续回喂


# =====================================================================
# 问题 4：额外字段不拒绝
# extract 默认补全 additionalProperties:false → 模型扩展字段被拒
# =====================================================================

SCHEMA_EXTRA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


@pytest.mark.asyncio
async def test_extra_field_rejected_by_default():
    """模型返回额外字段（user_emotion）→ 默认补全 additionalProperties:false → 被拒降级。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) <= 3:  # 第一级 + 回喂 2 次（均带额外字段，同约束被拒）
            return _sr(json.dumps({"name": "张三", "user_emotion": "开心"}))
        return _sr(json.dumps({"name": "张三"}))  # 第二级成功（无额外字段）

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_EXTRA)
    assert result == {"name": "张三"}
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_schema"  # 回喂1仍被拒
    assert calls[2]["type"] == "json_schema"  # 回喂2仍被拒
    assert calls[3]["type"] == "json_object"  # 降级第二级成功


@pytest.mark.asyncio
async def test_caller_schema_not_polluted():
    """extract 补全不污染调用方 schema（深拷贝）。"""
    from app.integration.llm.structured import StructuredOutput

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    enforced = enforce_no_extra_fields(schema)
    assert enforced["additionalProperties"] is False
    assert "additionalProperties" not in schema  # 调用方 schema 未被就地修改


@pytest.mark.asyncio
async def test_explicit_true_respected():
    """调用方显式写 additionalProperties:true → 尊重意图，不覆盖。"""
    from app.integration.llm.structured import StructuredOutput

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": True,  # 显式允许扩展
    }
    enforced = enforce_no_extra_fields(schema)
    assert enforced["additionalProperties"] is True  # 保持 true


@pytest.mark.asyncio
async def test_nested_objects_recursively_enforced():
    """嵌套 object 也递归补全 additionalProperties:false。"""
    from app.integration.llm.structured import StructuredOutput

    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "address": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        "required": ["name"],
    }
    enforced = enforce_no_extra_fields(schema)
    assert enforced["additionalProperties"] is False  # 顶层
    assert enforced["properties"]["address"]["additionalProperties"] is False  # 嵌套


# =====================================================================
# 审核修复：正则定位 JSON 块 / 可空对象形态
# =====================================================================


@pytest.mark.asyncio
async def test_fallback_regex_extracts_json_from_prose():
    """第三级 prose 包裹 JSON（前后有说明文字）→ 正则定位 JSON 块成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) <= 2:  # 第一级 + 回喂 2 次
            return _sr("bad json")
        if len(calls) <= 5:  # 第二级 + 回喂 2 次
            return _sr("bad json")
        # 第三级：prose 包裹（正则定位 {..} 提取）
        return _sr("这是结果：{\"name\": \"王五\"} 希望对你有帮助")

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "王五"}
    # 第一级(1)+回喂(2) + 第二级(1)+回喂(2) + 第三级(1) = 7
    assert len(calls) == 7


@pytest.mark.asyncio
async def test_enforce_nullable_object_type_array():
    """type 数组含 object（可空写法 ["object","null"]）也补全 additionalProperties:false。"""
    from app.integration.llm.structured import StructuredOutput

    schema = {
        "type": ["object", "null"],  # 可空对象
        "properties": {"name": {"type": "string"}},
    }
    enforced = enforce_no_extra_fields(schema)
    assert enforced["additionalProperties"] is False


# =====================================================================
# 日志脱敏：_validate_schema 校验失败日志不落完整模型输出（防敏感数据泄露）
# =====================================================================


def test_validate_schema_log_truncates_parsed(caplog):
    """_validate_schema 校验失败日志截断 parsed——不落完整模型输出。

    修复前：WARNING 日志含 `json.dumps(parsed)` 全量——结构化输出可能含业务
    敏感数据（Yield RCA 场景为良率/晶圆数据），全量落盘到日志是泄露面。
    修复后：parsed 截断到安全长度（仅错误摘要 + 前 N 字符），schema 保留完整。
    """
    from app.integration.llm.structured import _validate_schema

    # 超长敏感串作为 name 值（模拟良率/晶圆敏感内容），maxLength 约束触发校验失败
    long_secret = "yield=99.7,wafer=W12345-ABCD," * 50
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "maxLength": 10}},
        "required": ["name"],
    }
    parsed = {"name": long_secret}

    with caplog.at_level("WARNING", logger="app.llm.structured"):
        ok = _validate_schema(parsed, schema)
    assert not ok, "schema 校验应失败（name 超 maxLength）"
    assert long_secret not in caplog.text, "日志不应包含完整敏感模型输出"
    # 校验失败原因（validator 名）仍应保留——可观测性不因脱敏而丢失
    assert "maxLength" in caplog.text, "错误摘要（校验器 maxLength）应保留"


def test_validate_schema_log_keeps_schema(caplog):
    """schema（接口契约）保留完整——脱敏只针对模型输出，不损诊断。"""
    from app.integration.llm.structured import _validate_schema

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    parsed = {"name": 123}  # 类型错误触发校验失败

    with caplog.at_level("WARNING", logger="app.llm.structured"):
        ok = _validate_schema(parsed, schema)
    assert not ok
    assert '"name": {"type": "string"}' in caplog.text, "schema 契约应保留在日志"


def test_reask_log_truncates_instance_values(caplog):
    """回喂循环的校验失败日志不落完整实例值（脱敏），回喂模型仍带完整错误。

    修复前：_collect_schema_errors 用 e.message 拼接错误文本，日志（回喂第 N 次）
    同样嵌入完整实例值——Yield RCA 场景的敏感数据经此落盘。
    修复后：日志改用结构化字段摘要（路径 + validator + 约束值，无实例数据），
    回喂模型保留 e.message（模型需要具体错误才能修正）。
    """
    from app.integration.llm.structured_codec import collect_schema_errors

    long_secret = "yield=99.7,wafer=W12345-ABCD," * 50
    parsed = {"name": long_secret}
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "maxLength": 10}},
        "required": ["name"],
    }
    errors = collect_schema_errors(parsed, schema)
    # 回喂给模型的错误文本保留 e.message（含实例值，模型需要具体错误修正）
    assert long_secret in errors[0], "回喂模型的错误应保留 e.message（含实例值）"
    # 但错误文本本身不应被直接落盘——日志环节走脱敏摘要（见 generate_structured 集成路径）


@pytest.mark.asyncio
async def test_reask_log_path_truncates_instance_values(caplog):
    """完整 generate_structured 路径：回喂日志不落完整实例值（敏感数据）。

    修复前：_try_extract 的 logger.warning("...回喂第 %d 次: %s", "\n".join(errors))
    直接把含 e.message（嵌入完整实例值）的 errors 落盘。
    修复后：日志用脱敏摘要（字段路径 + validator + 约束值），无实例数据。
    """
    llm = LLMService()
    calls = []
    long_secret = "yield=99.7,wafer=W12345-ABCD," * 50

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        # 第一级 + 回喂 2 次均返回超长敏感值（校验失败）→ 耗尽降级第二级
        if len(calls) <= 3:
            return _sr(json.dumps({"name": long_secret}))
        return _sr(json.dumps({"name": "李四"}, ensure_ascii=False))  # 第二级成功

    llm.generate = fake_generate
    with caplog.at_level("WARNING", logger="app.llm.structured"):
        result = await llm.generate_structured(MESSAGES, {
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 10}},
            "required": ["name"],
        })
    assert result == {"name": "李四"}, "降级到第二级应成功"
    # 回喂日志不应包含完整敏感串（caplog 聚合全部 WARNING）
    assert long_secret not in caplog.text, "回喂日志不应包含完整敏感实例值"
    # 校验错误摘要（validator 名）仍应保留——可观测性不因脱敏而丢失
    assert "maxLength" in caplog.text, "脱敏摘要（validator 名）应保留在日志"


# =====================================================================
# 取消 / 期限信号下沉（E：结构化内部子调用闭环）
# =====================================================================


@pytest.mark.asyncio
async def test_cancel_before_first_call_returns_none_no_sdk():
    """cancel_event 已置位 → 首笔子调用前拦截，return None 且 SDK generate 0 次。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    cancel_event = asyncio.Event()
    cancel_event.set()
    result = await llm.generate_structured(MESSAGES, SCHEMA, cancel_event=cancel_event)
    assert result is None
    assert calls == []


async def test_call_generate_pre_cancel_raises_typed_abort_to_stop_fallback_chain():
    """子调用入口已取消时应抛类型化终止，由 extract 最外层一次性收敛。"""
    event = asyncio.Event()
    event.set()
    llm = LLMService()

    with pytest.raises(LLMCancelledError):
        await StructuredOutput._call_generate(
            llm_service=llm,
            messages=[{"role": "user", "content": "x"}],
            model_key="fast",
            max_tokens=32,
            cancel_event=event,
        )


@pytest.mark.asyncio
async def test_cancel_mid_chain_stops_degrade_no_further_sdk():
    """降级链中途 cancel：level1 失败后将置位 → 不再发 level2/3，仅 1 次 SDK 调用。"""
    llm = LLMService()
    calls = []
    cancel_event = asyncio.Event()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        if len(calls) == 1:
            cancel_event.set()  # 首次（level1）成功返回后置位——回喂/降级均应被拦截
            return _sr("bad json")
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA, cancel_event=cancel_event)
    assert result is None
    assert len(calls) == 1, "cancel 后不得再发起任何真实请求"


@pytest.mark.asyncio
async def test_cancel_truncation_retry_not_issued():
    """截断扩容重试前 cancel → 不追加扩容调用。"""
    llm = LLMService()
    calls = []
    cancel_event = asyncio.Event()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(max_tokens)
        cancel_event.set()  # 首次返回截断；扩容重试应在 _call_generate 入口被拦截
        return _sr("", finish_reason="length")

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA, cancel_event=cancel_event)
    assert result is None
    assert len(calls) == 1, "截断扩容重试前命中取消，不得再调用"


@pytest.mark.asyncio
async def test_deadline_passed_returns_none_no_sdk():
    """deadline 已过 → 拦截 return None，SDK 0 次。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(
        MESSAGES, SCHEMA, deadline=time.monotonic() - 1.0
    )
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_deadline_future_normal_path_unchanged():
    """对照：deadline 远在未来 → 正常走链，行为不受信号参数影响。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        seen["response_format"] = response_format
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(
        MESSAGES, SCHEMA, deadline=time.monotonic() + 60.0
    )
    assert result == {"name": "张三"}
    assert seen["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_usage_kept_when_chain_cancelled_mid():
    """中断保留：降级链已有成功调用（含 usage）后被取消 → usage 已回填该笔，不丢弃。"""
    llm = LLMService()
    calls = []
    cancel_event = asyncio.Event()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        sr = _sr("bad json")
        if len(calls) == 1:
            cancel_event.set()
            sr.usage = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
        return sr

    llm.generate = fake_generate
    usage: dict = {}
    result = await llm.generate_structured(MESSAGES, SCHEMA, cancel_event=cancel_event, usage=usage)
    assert result is None
    assert len(calls) == 1
    assert usage == {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}


@pytest.mark.asyncio
async def test_builtin_timeout_reraises_not_degrade_to_next_level():
    """内置 TimeoutError 防御直抛：不得被当可恢复错误吞掉降级到下一级。

    防线：外层策略绝对截止（asyncio.timeout 到期抛内置 TimeoutError）若注入
    结构化链，须保留执行终止语义（todo §4），而非 classify RETRYABLE → 降级再调用。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        raise TimeoutError("整体 deadline 到期")

    llm.generate = fake_generate
    with pytest.raises(TimeoutError):
        await llm.generate_structured(MESSAGES, SCHEMA)
    assert len(calls) == 1, "TimeoutError 直抛，不得降级到第二级再调用"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "abort_exc",
    [LLMCancelledError, LLMDeadlineExceededError],
    ids=["cancelled", "deadline"],
)
async def test_typed_abort_with_usage_accumulates_and_stops_degrade(abort_exc):
    """携 usage 的 shared 终止（cancel / deadline）→ usage 累计到调用方 + raise 给
    extract 最外层收敛 return None，剩余降级级不再发起（LLM-047 #6）。

    回归护栏：_call_generate 的 except 分支对携 usage 的终止异常先 _accumulate_usage
    再 raise——若只在成功返回路径累计，deadline 命中时已完成的这笔真实调用用量即丢
    （成本护栏低估），且终止被吞掉会空转 level2/3 降级。
    """
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast", cancel_event=None, deadline=None):
        calls.append(response_format)
        raise abort_exc(
            "执行终止",
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        )

    llm.generate = fake_generate
    usage: dict = {}
    result = await llm.generate_structured(MESSAGES, SCHEMA, usage=usage)
    assert result is None, "extract 最外层把执行终止收敛为 None（与降级耗尽同出口）"
    assert len(calls) == 1, "携 usage 终止后不得再发 level2/3（停止降级）"
    assert usage == {
        "prompt_tokens": 5,
        "completion_tokens": 3,
        "total_tokens": 8,
    }, "异常携带的 usage 应累计回填调用方（LLM-047 成本不丢）"
