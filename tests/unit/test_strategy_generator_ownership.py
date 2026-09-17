"""策略运行权必须覆盖内部生成器的关闭；非法批次不得部分执行。"""

import asyncio

import pytest

from app.domain.reasoning._common import reject_concurrent_runs
from app.domain.reasoning.react import ReActStrategy
from tests.reasoning_execution import reasoning_run_scope


async def test_running_guard_owns_inner_generator_until_cleanup_finishes():
    class Strategy:
        def __init__(self):
            self.closing = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False

        @reject_concurrent_runs
        async def execute(self):
            try:
                yield "event"
            finally:
                self.closing.set()
                await self.release.wait()
                self.closed = True

    strategy = Strategy()
    stream = strategy.execute()
    await anext(stream)
    closing = asyncio.create_task(stream.aclose())
    try:
        await asyncio.wait_for(strategy.closing.wait(), 1)
        assert not closing.done()
        assert strategy._execution_running
        with pytest.raises(RuntimeError, match="不能并发执行"):
            await anext(strategy.execute())
    finally:
        strategy.release.set()
        await closing
        await asyncio.sleep(0)
    assert strategy.closed
    assert not strategy._execution_running


@pytest.mark.parametrize("ids", [["duplicate", "duplicate"], ["valid", ""], ["valid", None]])
async def test_direct_tool_batch_rejects_all_calls_before_any_execution(ids):
    class Gateway:
        async def execute(self, *args, **kwargs):
            pytest.fail("非法批次不能执行任何工具")

    strategy = ReActStrategy(llm=None, tools=Gateway())
    messages = []
    calls = [{"id": call_id, "function": {"name": "write", "arguments": "{}"}} for call_id in ids]
    with pytest.raises(ValueError, match="协议异常"):
        async for _ in strategy.execute_tool_calls(calls, messages, 1, run=reasoning_run_scope("run")):
            pass
    assert messages == []
    assert strategy.tool_facts == ()
