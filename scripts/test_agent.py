#!/usr/bin/env python
"""
Agent 模块快速验证脚本

运行：
    cd 项目根目录
    uv run python -m scripts.test_agent
"""

import asyncio
import json
import sys

# Windows 控制台默认 GBK，统一切换为 UTF-8，避免打印 LLM 内容（可能含 emoji）时崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings
from app.domain.agent import AgentContext
from app.domain.agent.executor import ReActAgent
from app.domain.prompts.manager import PromptManager
from app.integration.tools.tool_service import ToolService
from app.integration.llm.llm_service import LLMService
from app.integration.tools.builtin import ReadFileTool, SearchTool, WriteFileTool


async def main():
    # 1. 准备依赖
    tools = ToolService()
    # 文件工具允许目录（默认项目根），避免真实执行 readFile/writeFile 被白名单拦截
    ReadFileTool.register_config(allowed_dirs=settings.tool_allowed_dirs)
    WriteFileTool.register_config(allowed_dirs=settings.tool_allowed_dirs)
    for tool_cls in [SearchTool, ReadFileTool, WriteFileTool]:
        tools.register(tool_cls())

    print(f"已注册工具: {tools.list_tools()}")
    print(f"LLM 模型: {settings.llm_model_id}")
    print()

    # 2. 构建上下文
    ctx = AgentContext(
        session_id="test_session",
        user_id="test_user",
        max_iterations=5,
    )

    # 3. 构建消息
    pm = PromptManager()
    tools_desc = "\n".join(
        f"- {t.name}: {t.description[:50]}..." for t in tools._tools.values()
    )
    system_prompt = pm.build_system_prompt(tools_desc)
    messages = [{"role": "system", "content": system_prompt}]

    # 4. 创建 Agent
    llm = LLMService(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_model_id,
    )
    agent = ReActAgent(llm=llm, tools=tools)

    # 5. 运行（流式输出）
    user_input = "你好，介绍一下你自己有哪些能力？"
    messages.append({"role": "user", "content": user_input})

    print(f"用户: {user_input}")
    print("Agent: ", end="", flush=True)

    async for event in agent.run(user_input, messages, context=ctx):
        if isinstance(event, str):
            try:
                data = json.loads(event[6:])  # 去掉 "data: " 前缀
                t = data.get("type", "")
                c = data.get("content", "")
                if t == "message":
                    print(c, end="", flush=True)
                elif t == "reasoning":
                    pass  # 不输出思考过程
                elif t == "tool_call":
                    print(f"\n  [TOOL] 调用工具: {c}", flush=True)
                elif t == "tool_result":
                    print(f"    结果: {c[:80]}...", flush=True)
                elif t == "done":
                    print(f"\n  [OK] 完成（{data.get('iterations', 0)} 轮）", flush=True)
                elif t == "error":
                    print(f"\n  [FAIL] 错误: {c}", flush=True)
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

    # 6. 显示最终结果
    result = agent.result
    if result:
        print()
        print("最终结果:")
        print(f"  成功: {result.success}")
        print(f"  思考：{result.reasoning[:100]}...")
        print(f"  内容: {result.content[:100]}...")
        print(f"  迭代轮数: {result.iterations}")
        print(f"  工具调用: {len(result.tool_calls)} 次")
        print(f"  Token 用量: {result.total_tokens}")


if __name__ == "__main__":
    asyncio.run(main())
