"""
ToolExecutor 组件集成测试（经 ToolService Facade）

覆盖：
    参数校验失败 → 可归因错误（非 kwargs 转储）
    成功结果按 tool.max_output_length 统一截断
    concurrency_safe=False 同工具串行化 / True 可并发
    审计在 success / failure / validation / not-found 各路径各记录一条
"""

import asyncio

import pytest

from app.domain.ports.tool_gateway import ErrorCode
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.executor import ToolExecutor
from app.integration.tools.hooks import ExecutionHooks
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.security import RiskLevel, ToolAuditor
from app.integration.tools.stats import ToolStatsCollector
from app.integration.tools.tool_service import ToolService


class _ConcurrentTool(BaseTool):
    """可配置并发安全的测试工具，观测 max_active 并发度。"""

    def __init__(self, *, concurrency_safe: bool = True, delay: float = 0.02):
        self._safe = concurrency_safe
        self.delay = delay
        self.active = 0
        self.max_active = 0

    @property
    def name(self) -> str:
        return "c_tool"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def concurrency_safe(self) -> bool:
        return self._safe

    async def execute(self, **kwargs) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        return ToolResult(success=True, content="done")


class _ParamTool(BaseTool):
    """带参数 schema 的测试工具：count 必填 integer。"""

    @property
    def name(self) -> str:
        return "param_tool"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="ok")


class _BigOutputTool(_ParamTool):
    """超大输出工具：max_output_length=100。"""

    @property
    def max_output_length(self) -> int:
        return 100

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, content="A" * 1000)


class _SpyAuditor(ToolAuditor):
    """记录审计调用的 spy（验证各路径审计触发）。"""

    def __init__(self) -> None:
        super().__init__(enabled=True)
        self.records: list[dict] = []

    async def record(self, **kwargs) -> None:  # noqa: A003
        self.records.append(kwargs)


@pytest.mark.asyncio
async def test_validation_failure_reports_attribution():
    """校验失败 → error 含具体归因（类型应为 integer），非 kwargs 转储。"""
    service = ToolService()
    service.register(_ParamTool())

    result = await service.execute("param_tool", {"count": "3"})

    assert result.success is False
    assert "参数验证失败" in result.error
    assert "类型应为 integer" in result.error
    assert result.error_code == ErrorCode.VALIDATION


@pytest.mark.asyncio
async def test_success_result_truncated_by_tool_max_length():
    """成功结果按 tool.max_output_length 统一 head+tail 截断。"""
    service = ToolService()
    service.register(_BigOutputTool())

    result = await service.execute("param_tool", {"count": 1})

    assert result.success is True
    assert "已截断" in result.content
    assert result.metadata["truncated"] is True


@pytest.mark.asyncio
async def test_concurrency_safe_false_serializes_same_tool():
    """concurrency_safe=False 同工具并发串行化（max_active==1）。"""
    tool = _ConcurrentTool(concurrency_safe=False, delay=0.02)
    service = ToolService(max_concurrent_tools=5)
    service.register(tool)

    await asyncio.gather(*[service.execute("c_tool", {}) for _ in range(5)])

    assert tool.max_active == 1, f"非并发安全工具应串行化，实际并发 {tool.max_active}"


@pytest.mark.asyncio
async def test_concurrency_safe_true_allows_parallel():
    """concurrency_safe=True 允许并行（max_active > 1）。"""
    tool = _ConcurrentTool(concurrency_safe=True, delay=0.02)
    service = ToolService(max_concurrent_tools=5)
    service.register(tool)

    await asyncio.gather(*[service.execute("c_tool", {}) for _ in range(5)])

    assert tool.max_active > 1


@pytest.mark.asyncio
async def test_audit_recorded_on_success():
    """成功路径审计 1 条，含工具元数据。"""
    spy = _SpyAuditor()
    service = ToolService(auditor=spy)
    service.register(_ParamTool())

    await service.execute("param_tool", {"count": 1})

    assert len(spy.records) == 1
    assert spy.records[0]["tool_name"] == "param_tool"
    assert spy.records[0]["success"] is True
    assert spy.records[0]["risk_level"] == RiskLevel.L0_READONLY


@pytest.mark.asyncio
async def test_audit_on_not_found_tool():
    """未注册工具审计 1 条（risk 兜底 L0，tool_name 保留传入名）。"""
    spy = _SpyAuditor()
    service = ToolService(auditor=spy)

    result = await service.execute("ghost", {})

    assert result.success is False
    assert result.error_code == ErrorCode.NOT_REGISTERED
    assert len(spy.records) == 1
    assert spy.records[0]["tool_name"] == "ghost"
    assert spy.records[0]["success"] is False
    assert spy.records[0]["risk_level"] == RiskLevel.L0_READONLY


@pytest.mark.asyncio
async def test_audit_on_validation_failure():
    """校验失败路径审计 1 条，error 含归因。"""
    spy = _SpyAuditor()
    service = ToolService(auditor=spy)
    service.register(_ParamTool())

    await service.execute("param_tool", {"count": "3"})

    assert len(spy.records) == 1
    assert spy.records[0]["success"] is False
    assert "参数验证失败" in spy.records[0]["error"]


@pytest.mark.asyncio
async def test_audit_on_tool_failure():
    """工具执行失败（重试耗尽后）审计 1 条。"""

    class _FailTool(_ParamTool):
        async def execute(self, **kwargs) -> ToolResult:
            return ToolResult(success=False, content="", error="业务失败")

    spy = _SpyAuditor()
    service = ToolService(auditor=spy, tool_max_retries=1)  # 不重试，快速失败
    service.register(_FailTool())

    result = await service.execute("param_tool", {"count": 1})

    assert result.success is False
    assert result.error_code is None  # 业务失败透传工具业务码（默认 None）
    assert len(spy.records) == 1
    assert spy.records[0]["success"] is False
    assert "业务失败" in spy.records[0]["error"]


class _SlowTool(_ParamTool):
    """慢工具：声明 timeout 属性，execute 固定 sleep 模拟耗时调用。"""

    def __init__(self, *, declared_timeout: int | None = None):
        self._declared_timeout = declared_timeout

    @property
    def timeout(self) -> int | None:
        return self._declared_timeout

    async def execute(self, **kwargs) -> ToolResult:
        await asyncio.sleep(0.2)
        return ToolResult(success=True, content="slow done")


@pytest.mark.asyncio
async def test_tool_declared_timeout_takes_precedence_over_global():
    """工具自声明 timeout（0.05s）优先于全局配置（30s）→ 慢执行超时失败。"""
    tool = _SlowTool(declared_timeout=0.05)
    service = ToolService(tool_max_retries=1)
    service.register(tool)

    result = await service.execute("param_tool", {"count": 1})

    assert result.success is False
    assert "超时" in result.error


@pytest.mark.asyncio
async def test_caller_timeout_overrides_tool_declared():
    """调用方显式 timeout（0.05s）覆盖工具自声明（1s）→ 慢执行超时失败。"""
    tool = _SlowTool(declared_timeout=1)
    service = ToolService(tool_max_retries=1)
    service.register(tool)

    result = await service.execute("param_tool", {"count": 1}, timeout=0.05)

    assert result.success is False
    assert "超时" in result.error


@pytest.mark.asyncio
async def test_global_timeout_used_when_tool_declares_none():
    """工具声明 None → 沿用全局配置（注入小值）→ 慢执行超时失败。"""
    tool = _SlowTool(declared_timeout=None)
    service = ToolService(tool_timeout=0.05, tool_max_retries=1)
    service.register(tool)

    result = await service.execute("param_tool", {"count": 1})

    assert result.success is False
    assert "超时" in result.error
    assert result.error_code == ErrorCode.TIMEOUT


@pytest.mark.asyncio
async def test_uncaught_exception_maps_to_unknown():
    """工具 execute 抛未捕获异常 → error_code=UNKNOWN。"""

    class _ExplodingTool(_ParamTool):
        async def execute(self, **kwargs) -> ToolResult:
            raise RuntimeError("boom")

    service = ToolService(tool_max_retries=1)
    service.register(_ExplodingTool())

    result = await service.execute("param_tool", {"count": 1})

    assert result.success is False
    assert result.error_code == ErrorCode.UNKNOWN


@pytest.mark.asyncio
async def test_prune_tool_lock_skips_held():
    """外部工具重载场景：在飞 execute 持锁时 prune 跳过；释放后才清理。"""
    executor = ToolExecutor(ToolRegistry(), ToolStatsCollector(), ExecutionHooks())
    lock = asyncio.Lock()
    executor._tool_locks["x"] = lock
    await lock.acquire()

    executor.prune_tool_lock("x")
    assert "x" in executor._tool_locks  # 持锁跳过 → 重载后新实例复用同一把锁

    lock.release()
    executor.prune_tool_lock("x")
    assert "x" not in executor._tool_locks
