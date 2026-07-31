"""
StructuredOutput — 结构化输出支持

职责：
    1. 根据 JSON Schema 从 LLM 输出中提取结构化数据
    2. 优先使用原生 response_format（JSON Schema）
    3. 降级：模型不支持时使用 prompt 约束

使用方式：
    result = await StructuredOutput.extract(
        llm=llm_service,
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
        prompt="从以下内容中提取人名",
        content="张三去了北京",
    )
    # → {"name": "张三"}
"""

from __future__ import annotations

import json
import re
from typing import Any


class StructuredOutput:
    """
    结构化输出提取器。

    优先使用 OpenAI 原生 JSON Schema（response_format），
    兜底使用 prompt 约束 + 正则提取。
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
        schema: dict[str, Any],
        prompt: str,
        content: str,
        model_key: str = "fast",
    ) -> dict[str, Any] | None:
        """
        根据 JSON Schema 从内容中提取结构化数据。

        Args:
            llm_service: LLMService 实例
            schema: JSON Schema 定义
            prompt: 提取指令 prompt
            content: 待提取的源内容
            model_key: 使用的模型标识（默认 fast，低延迟低成本）

        Returns:
            解析后的 dict，失败返回 None
        """
        messages = [
            {
                "role": "system",
                "content": (
                    f"{prompt}\n\n"
                    f"请严格按照以下 JSON Schema 返回数据：\n"
                    f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
                    f"只返回 JSON，不要包含 Markdown 代码块或其他说明。"
                ),
            },
            {"role": "user", "content": content},
        ]

        # 先用原生 JSON Schema
        response_format = StructuredOutput.build_json_schema_request(schema)
        result = await StructuredOutput._try_extract(
            llm_service, messages, response_format, model_key,
        )
        if result is not None:
            return result

        # 降级：普通 JSON mode
        response_format = StructuredOutput.build_json_mode_request()
        result = await StructuredOutput._try_extract(
            llm_service, messages, response_format, model_key,
        )
        if result is not None:
            return result

        # 最终降级：纯 prompt 约束 + 正则提取
        return await StructuredOutput._fallback_extract(
            llm_service, messages, model_key,
        )

    @staticmethod
    async def _try_extract(
        llm_service: Any,
        messages: list[dict],
        response_format: dict,
        model_key: str,
    ) -> dict[str, Any] | None:
        """尝试用指定 response_format 提取。"""
        try:
            result = await llm_service.generate(
                messages=messages,
                temperature=0,
                max_tokens=2048,
                response_format=response_format,
                model_key=model_key,
            )
            if result and result.content:
                parsed = json.loads(result.content)
                if isinstance(parsed, dict):
                    return parsed
        except (json.JSONDecodeError, Exception):
            pass
        return None

    @staticmethod
    async def _fallback_extract(
        llm_service: Any,
        messages: list[dict],
        model_key: str,
    ) -> dict[str, Any] | None:
        """纯 prompt 约束降级方案。"""
        try:
            result = await llm_service.generate(
                messages=messages,
                temperature=0,
                max_tokens=2048,
                model_key=model_key,
            )
            if not result or not result.content:
                return None
            # 尝试提取 JSON 块
            content = result.content.strip()
            # 移除 markdown 代码块标记
            content = re.sub(
                r"^```(?:json)?\s*", "", content, flags=re.MULTILINE,
            )
            content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
            return json.loads(content)
        except (json.JSONDecodeError, Exception):
            return None
