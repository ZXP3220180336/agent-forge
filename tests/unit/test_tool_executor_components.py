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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_json", ["[1,2,3]", "null", "42", '"str"', "true"]
)
async def test_execute_non_dict_json_rejected(bad_json):
    """LLM 返回数组/标量/null 参数时归 JSON_PARSE，不逃逸 TypeError。"""
    service = ToolService()
    service.register(_ParamTool())

    result = await service.execute("param_tool", bad_json)

    assert result.success is False
    assert result.error_code == ErrorCode.JSON_PARSE
    assert "JSON 对象" in result.error


@pytest.mark.asyncio
async def test_execute_non_str_key_dict_rejected():
    """含非 str 键的 dict 参数归 JSON_PARSE（**parameters 会 TypeError）。"""
    service = ToolService()
    service.register(_ParamTool())

    result = await service.execute("param_tool", {1: "a"})

    assert result.success is False
    assert result.error_code == ErrorCode.JSON_PARSE
    assert "JSON 对象" in result.error


@pytest.mark.asyncio
async def test_execute_list_parameters_rejected():
    """直接传 list 参数归 JSON_PARSE（非 str 分支同样校验）。"""
    service = ToolService()
    service.register(_ParamTool())

    result = await service.execute("param_tool", [1, 2])

    assert result.success is False
    assert result.error_code == ErrorCode.JSON_PARSE


class _FlakyTool(_ParamTool):
    """前 fail_times 次执行失败，之后成功。"""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ToolResult(success=False, content="", error="flaky")
        return ToolResult(success=True, content="ok")


class _AlwaysFailTool(_ParamTool):
    """始终失败。"""

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=False, content="", error="always fail")


@pytest.mark.asyncio
async def test_retry_count_success_path_is_executions():
    """成功路径 retry_count = 实际执行次数（前 2 次失败，第 3 次成功 → 3）。"""
    service = ToolService(tool_max_retries=3)
    service.register(_FlakyTool(fail_times=2))

    result = await service.execute("param_tool", {"count": 1}, retry_delay=0)

    assert result.success is True
    assert result.retry_count == 3  # 3 次尝试成功，非 0 基索引 2


@pytest.mark.asyncio
async def test_retry_count_failure_path_is_executions():
    """全败路径 retry_count = 实际执行次数（3 次全败 → 3，与成功路径口径一致）。"""
    service = ToolService(tool_max_retries=3)
    service.register(_AlwaysFailTool())

    result = await service.execute("param_tool", {"count": 1}, retry_delay=0)

    assert result.success is False
    assert result.retry_count == 3


@pytest.mark.asyncio
async def test_execute_max_retries_zero_runs_once():
    """max_retries=0 视为「不重试跑一次」，工具至少执行一次（而非零次循环）。"""
    service = ToolService()
    flaky = _FlakyTool(fail_times=0)  # 立即成功
    service.register(flaky)

    result = await service.execute("param_tool", {"count": 1}, max_retries=0)

    assert result.success is True
    assert flaky.calls == 1  # 执行了一次
    assert result.retry_count == 1


@pytest.mark.asyncio
async def test_execute_max_retries_zero_failure_not_silent():
    """max_retries=0 失败时返回真实归因错误（非静默 success=False, error=None）。"""
    service = ToolService()
    service.register(_AlwaysFailTool())

    result = await service.execute("param_tool", {"count": 1}, max_retries=0)

    assert result.success is False
    assert result.error == "always fail"  # 有归因错误，非静默空失败
    assert result.retry_count == 1
