"""测试直连工具入口：为旧行为用例显式建立独立生命周期身份。"""

from __future__ import annotations

import asyncio
import uuid

from app.domain.ports.tool_execution import ToolCallContext
from app.domain.reasoning.tool_batch import ToolBatchCollector
from app.integration.tools.executor import ToolExecutor
from app.integration.tools.tool_service import ToolService


def execution_kwargs():
    suffix = uuid.uuid4().hex
    return {
        "call": ToolCallContext(
            run_id=f"test-run-{suffix}",
            batch_id=f"test-batch-{suffix}",
            tool_call_id=f"test-call-{suffix}",
            operation_id=f"test-operation-{suffix}",
            cancel_events=(asyncio.Event(),),
            run_stop=asyncio.Event(),
        ),
        "facts": ToolBatchCollector(),
    }


class StandaloneToolService(ToolService):
    """测试脚本式入口；每次调用显式委托一组独立身份。"""

    async def execute(self, *args, **kwargs):
        defaults = execution_kwargs()
        kwargs.setdefault("call", defaults["call"])
        kwargs.setdefault("facts", defaults["facts"])
        return await super().execute(*args, **kwargs)


class StandaloneToolExecutor(ToolExecutor):
    """直接 Executor 单测的显式生命周期适配器。"""

    async def execute(self, *args, **kwargs):
        defaults = execution_kwargs()
        kwargs.setdefault("call", defaults["call"])
        kwargs.setdefault("facts", defaults["facts"])
        return await super().execute(*args, **kwargs)
