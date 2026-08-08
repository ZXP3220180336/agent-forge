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

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}
MESSAGES = [{"role": "user", "content": "张三去了北京"}]


def _sr(content: str | None) -> StreamResult:
    """构造带指定 content 的 StreamResult。"""
    sr = StreamResult()
    sr.content = content or ""
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
    """第一级失败 → 第二级 json_object 成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr("not valid json {")  # 第一级解析失败
        return _sr(json.dumps({"name": "李四"}, ensure_ascii=False))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "李四"}
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_object"


@pytest.mark.asyncio
async def test_third_level_regex_fallback():
    """前两级失败 → 第三级正则 fallback 成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        calls.append(response_format)
        if len(calls) <= 2:
            return _sr("bad json")
        return _sr("```json\n{\"name\": \"王五\"}\n```")  # 无 response_format，带代码块

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA)
    assert result == {"name": "王五"}
    # 第三级无 response_format（fallback 调用 generate 不传 response_format）
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_object"
    assert calls[2] is None
    assert len(calls) == 3


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
    """第一级的 json_schema 内含完整 schema。"""
    llm = LLMService()
    seen = {}

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        seen["response_format"] = response_format
        return _sr(json.dumps({"name": "张三"}, ensure_ascii=False))

    llm.generate = fake_generate
    await llm.generate_structured(MESSAGES, SCHEMA)
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA
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
    """第一级结构合法但 confidence 超范围（strict 不保证值约束）→ 校验失败 → 降级第二级成功。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr(json.dumps({"name": "张三", "confidence": 5}))
        return _sr(json.dumps({"name": "张三", "confidence": 0.9}))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "张三", "confidence": 0.9}
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_object"


@pytest.mark.asyncio
async def test_schema_validation_missing_required_falls_back():
    """第一级缺必填字段 confidence → 校验失败 → 降级。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        calls.append(response_format)
        if len(calls) == 1:
            return _sr(json.dumps({"name": "张三"}))
        return _sr(json.dumps({"name": "张三", "confidence": 0.8}))

    llm.generate = fake_generate
    result = await llm.generate_structured(MESSAGES, SCHEMA_RANGE)
    assert result == {"name": "张三", "confidence": 0.8}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_schema_validation_all_levels_fail_returns_none():
    """三级均返回结构合法但不合 schema → 全部校验失败 → None（含第三级正则路径也校验）。"""
    llm = LLMService()
    calls = []

    async def fake_generate(messages, temperature, max_tokens, response_format=None, model_key="fast"):
        calls.append(response_format)
        return _sr(json.dumps({"name": "张三", "confidence": "很高"}))

    llm.generate = fake_generate
    assert await llm.generate_structured(MESSAGES, SCHEMA_RANGE) is None
    assert len(calls) == 3
