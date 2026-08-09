"""
LLMService.generate_structured 单元测试

覆盖「统一结构化输出入口」决策（generate_structured 委托 StructuredOutput 三级降级）：
    第一级：原生 JSON Schema（strict=True）
    第二级：JSON Mode（json_object）
    第三级：纯 prompt 约束 + 正则提取

不依赖真实 API：mock LLMService.generate（async），构造 StreamResult 返回。
通过真实委托验证 generate_structured 内部走三级降级（断言各级 generate 调用参数）。
"""

import json

import pytest

from app.services.llm_service import LLMService, StreamResult
from app.services.llm.structured import (
    StructuredRefusalError,
    StructuredTruncationError,
)

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}
MESSAGES = [{"role": "user", "content": "张三去了北京"}]


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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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


# =====================================================================
# 失败路径
# =====================================================================


@pytest.mark.asyncio
async def test_json_parse_failure_returns_none():
    """三级均返回非法 JSON → None。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        return _sr("definitely not json")

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None


@pytest.mark.asyncio
async def test_non_dict_content_returns_none():
    """content 是 JSON 数组（非 dict）→ None（isinstance dict 校验）。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        return _sr("[1, 2, 3]")

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None


@pytest.mark.asyncio
async def test_empty_response_returns_none():
    """generate 返回 None / 空 content → None。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        return _sr_none()

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA) is None


@pytest.mark.asyncio
async def test_generate_exception_returns_none():
    """generate 抛异常 → None（降级路径静默吞异常）。"""
    llm = LLMService()

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        raise RuntimeError("downstream failure")

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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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
async def test_truncation_retry_still_truncated_returns_none():
    """第一级截断 → 扩 token 重试仍截断 → 短路返回 None（不降级）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        calls.append(response_format)
        return _sr("", finish_reason="stop")

    llm.generate = fake_generate
    with pytest.raises(StructuredRefusalError):
        await llm.generate_structured(MESSAGES, SCHEMA)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_tool_calls_finish_not_treated_as_refusal():
    """finish_reason=tool_calls + content 空 → 短路抛 StructuredToolCallError。

    修复前（原始）：`_classify_result` 的 `if not result.content:` 未排除 tool_calls，
    工具调用响应（空 content 是正常形态）被误判为拒答抛 StructuredRefusalError。
    修复后：tool_calls 作为独立短路类别抛 StructuredToolCallError——模型已放弃输出
    JSON，降级到更宽松约束（JSON mode/纯 prompt）无意义，短路不进入降级链，
    交回调用方按工具调用处理。
    """
    from app.services.llm.structured import StructuredToolCallError

    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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
async def test_reask_exhausted_falls_back():
    """第一级回喂耗尽（2 次）仍校验失败 → 降级到第二级成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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


@pytest.mark.asyncio
async def test_reask_truncation_does_not_enter_loop():
    """回喂循环内截断 → 不进入回喂循环、不扩 token 组合 → 降级。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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
async def test_reask_refusal_short_circuits():
    """回喂循环内拒答 → 抛 StructuredRefusalError（不降级、不继续回喂）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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
    from app.services.llm.structured import StructuredOutput

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    enforced = StructuredOutput._enforce_no_extra_fields(schema)
    assert enforced["additionalProperties"] is False
    assert "additionalProperties" not in schema  # 调用方 schema 未被就地修改


@pytest.mark.asyncio
async def test_explicit_true_respected():
    """调用方显式写 additionalProperties:true → 尊重意图，不覆盖。"""
    from app.services.llm.structured import StructuredOutput

    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": True,  # 显式允许扩展
    }
    enforced = StructuredOutput._enforce_no_extra_fields(schema)
    assert enforced["additionalProperties"] is True  # 保持 true


@pytest.mark.asyncio
async def test_nested_objects_recursively_enforced():
    """嵌套 object 也递归补全 additionalProperties:false。"""
    from app.services.llm.structured import StructuredOutput

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
    enforced = StructuredOutput._enforce_no_extra_fields(schema)
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

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
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
    from app.services.llm.structured import StructuredOutput

    schema = {
        "type": ["object", "null"],  # 可空对象
        "properties": {"name": {"type": "string"}},
    }
    enforced = StructuredOutput._enforce_no_extra_fields(schema)
    assert enforced["additionalProperties"] is False
