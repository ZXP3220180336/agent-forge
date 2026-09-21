"""
ToolExecutor 组件集成测试（经 ToolService Facade）

覆盖：
    参数校验失败 → 可归因错误（非 kwargs 转储）
    成功结果按 tool.max_output_length 统一截断
    concurrency_safe=False 同工具串行化 / True 可并发
    审计在 success / failure / validation / not-found 各路径各记录一条
    准入拒绝：CAPACITY_EXCEEDED + NOT_STARTED 事实 + 审计一条，工具不执行
"""

import asyncio

import pytest

from app.domain.ports.tool_execution import ToolExecutionState
from app.domain.ports.tool_gateway import ErrorCode
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.execution import ToolEffectClass, ToolExecutionSpec
from app.integration.tools.hooks import ExecutionHooks
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.result_processor import ResultProcessor
from app.integration.tools.security import RiskLevel, ToolAuditor
from app.integration.tools.stats import ToolStatsCollector
from tests.observation_helpers import exploding_handler
from tests.tool_lifecycle import StandaloneToolExecutor as ToolExecutor
from tests.tool_lifecycle import StandaloneToolService as ToolService
from tests.tool_lifecycle import execution_kwargs


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

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

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

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

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

    async def record(self, **kwargs) -> None:
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


class _HoldingTool(_ParamTool):
    """占住在途名额的慢工具：execute 阻塞到 release 置位，用于制造排队与容量拒绝。"""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release
        self.started = asyncio.Event()
        self.calls = 0

    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.L1_WRITE

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        self.started.set()
        await self._release.wait()
        return ToolResult(success=True, content="done")


@pytest.mark.asyncio
async def test_capacity_rejection_yields_capacity_exceeded_with_audit_and_fact():
    """准入拒绝：CAPACITY_EXCEEDED + NOT_STARTED 终局事实 + 审计 1 条，工具未执行、不重试。"""
    release = asyncio.Event()
    spy = _SpyAuditor()
    service = ToolService(
        auditor=spy,
        max_concurrent_tools_per_run=1,
        max_concurrent_tools_global=1,
        max_pending_calls=1,
        max_pending_calls_per_run=1,
        admission_timeout_seconds=0.05,  # 队列满之外的兜底出口，避免测试挂在默认 30s
    )
    tool = _HoldingTool(release)
    service.register(tool)

    holder = asyncio.create_task(service.execute("param_tool", {"count": 1}))
    await tool.started.wait()

    queued = asyncio.create_task(service.execute("param_tool", {"count": 1}))
    async with asyncio.timeout(1):
        while service._executor._admission.pending != 1:
            await asyncio.sleep(0)
    assert service._executor._admission.pending == 1

    facts_kwargs = execution_kwargs()
    rejected = await service.execute("param_tool", {"count": 1}, **facts_kwargs)

    assert rejected.success is False
    assert rejected.error_code == ErrorCode.CAPACITY_EXCEEDED

    # 事实：终局保持 NOT_STARTED，并带上拒绝结果
    facts = facts_kwargs["facts"].snapshot()
    assert len(facts) == 1
    assert facts[0].execution_state == ToolExecutionState.NOT_STARTED
    assert facts[0].result is not None
    assert facts[0].result.error_code == ErrorCode.CAPACITY_EXCEEDED

    # 审计：拒绝也是 execute 的退出点，1 条，且风险级取注册表实际声明（非 L0 兜底）
    assert len(spy.records) == 1
    assert spy.records[0]["tool_name"] == "param_tool"
    assert spy.records[0]["error_code"] == ErrorCode.CAPACITY_EXCEEDED
    assert spy.records[0]["risk_level"] == RiskLevel.L1_WRITE

    # 工具只被 holder 进入过一次；拒绝的调用从未执行
    assert tool.calls == 1

    release.set()
    await asyncio.gather(holder, queued)


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
async def test_invalid_tool_result_is_normalized_without_retry():
    """适配器返回非 ToolResult 时收敛为 UNKNOWN，不逃逸也不重复执行。"""

    class _InvalidResultTool(_ParamTool):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return {"success": True, "content": "非法返回"}

        def can_retry(self, result_or_error):
            raise AssertionError("非法返回不应进入重试判断")

    tool = _InvalidResultTool()
    service = ToolService(tool_max_retries=3)
    service.register(tool)
    facts_kwargs = execution_kwargs()

    result = await service.execute("param_tool", {"count": 1}, **facts_kwargs)

    assert tool.calls == 1
    assert result.success is False
    assert result.error_code == ErrorCode.UNKNOWN
    assert "类型 dict" in result.error
    facts = facts_kwargs["facts"].snapshot()
    terminal = [fact for fact in facts if fact.attempt_id is not None][-1]
    assert terminal.execution_state == ToolExecutionState.FAILED
    assert terminal.cleanup_state.value == "COMPLETE"
    assert terminal.result is not None
    assert terminal.result.error_code == ErrorCode.UNKNOWN


@pytest.mark.parametrize(
    ("payload", "expected_reason"),
    [
        ({"success": True, "content": None}, "content"),
        ({"success": True, "content": 123}, "content"),
        ({"success": "yes", "content": "ok"}, "success"),
        ({"success": False, "content": "", "error": {"msg": "x"}}, "error"),
        ({"success": False, "content": "", "error_code": "TIMEOUT"}, "error_code"),
        ({"success": True, "content": "ok", "effect_state": "NONE"}, "effect_state"),
    ],
    ids=["content-None", "content-int", "success-str", "error-dict", "error_code-str", "effect_state-str"],
)
@pytest.mark.asyncio
async def test_invalid_tool_result_fields_are_normalized_without_retry(payload, expected_reason):
    """ToolResult 字段越界同样在真实调用边界收敛为 UNKNOWN，不逃逸也不重复执行。"""

    class _BadFieldTool(_ParamTool):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return ToolResult(**payload)

        def can_retry(self, result_or_error):
            raise AssertionError("非法返回不应进入重试判断")

    tool = _BadFieldTool()
    service = ToolService(tool_max_retries=3)
    service.register(tool)
    facts_kwargs = execution_kwargs()

    result = await service.execute("param_tool", {"count": 1}, **facts_kwargs)

    assert tool.calls == 1
    assert result.success is False
    assert result.error_code == ErrorCode.UNKNOWN
    assert expected_reason in result.error
    facts = facts_kwargs["facts"].snapshot()
    terminal = [fact for fact in facts if fact.attempt_id is not None][-1]
    assert terminal.execution_state == ToolExecutionState.FAILED
    assert terminal.result is not None
    assert terminal.result.error_code == ErrorCode.UNKNOWN


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
@pytest.mark.parametrize("bad_json", ["[1,2,3]", "null", "42", '"str"', "true"])
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

    def can_retry(self, result_or_error: ToolResult | BaseException) -> bool:
        return True


class _AlwaysFailTool(_ParamTool):
    """始终失败。"""

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=False, content="", error="always fail")

    def can_retry(self, result_or_error: ToolResult | BaseException) -> bool:
        return True


class _CountingSuccessTool(_ParamTool):
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, content="accepted")


class _ExplodingResultProcessor(ResultProcessor):
    def truncate_result(self, result: ToolResult, *, max_length: int | None = None) -> None:
        raise RuntimeError("truncate failed")


@pytest.mark.asyncio
async def test_success_postprocessing_failure_does_not_repeat_tool_execution():
    """展示处理失败保留原成功结果，也不得把真实调用送回重试循环。"""
    tool = _CountingSuccessTool()
    auditor = _SpyAuditor()
    service = ToolService(
        tool_max_retries=3,
        result_processor=_ExplodingResultProcessor(),
        auditor=auditor,
    )
    service.register(tool)

    result = await service.execute("param_tool", {"count": 1}, retry_delay=0)

    assert tool.calls == 1
    assert result.success is True
    assert result.content == "accepted"
    assert auditor.records[0]["success"] is True


@pytest.mark.asyncio
async def test_failure_is_not_retried_without_tool_safety_declaration():
    """次数预算不会单独授权重试；BaseTool 缺省必须拒绝自动重复。"""

    class _DefaultNoRetryTool(_ParamTool):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, **kwargs) -> ToolResult:
            self.calls += 1
            return ToolResult(success=False, content="", error="unsafe to repeat")

    tool = _DefaultNoRetryTool()
    service = ToolService(tool_max_retries=3)
    service.register(tool)

    result = await service.execute("param_tool", {"count": 1}, retry_delay=0)

    assert result.success is False
    assert result.retry_count == 1
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_retry_safety_check_failure_stops_without_hiding_tool_result():
    class _BrokenRetryCheckTool(_ParamTool):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, **kwargs) -> ToolResult:
            self.calls += 1
            return ToolResult(success=False, content="", error="original failure")

        def can_retry(self, result_or_error: ToolResult | BaseException) -> bool:
            raise RuntimeError("classification failed")

    tool = _BrokenRetryCheckTool()
    service = ToolService(tool_max_retries=3)
    service.register(tool)

    result = await service.execute("param_tool", {"count": 1}, retry_delay=0)

    assert tool.calls == 1
    assert result.error == "original failure"
    assert result.retry_count == 1


@pytest.mark.asyncio
async def test_hook_timeout_and_mutation_do_not_override_success_result():
    tool = _CountingSuccessTool()
    auditor = _SpyAuditor()
    service = ToolService(
        auditor=auditor,
        tool_observation_timeout=0.01,
    )
    service.register(tool)

    async def mutating_hook(name, parameters, result):
        result.success = False
        result.content = "mutated"

    async def hanging_hook(name, parameters, result):
        await asyncio.sleep(1)

    service.add_execution_hook(mutating_hook)
    service.add_execution_hook(hanging_hook)

    result = await service.execute("param_tool", {"count": 1})

    assert tool.calls == 1
    assert result.success is True
    assert result.content == "accepted"
    assert len(auditor.records) == 1  # 审计优先于可扩展 Hook 消费共享观察预算


@pytest.mark.asyncio
async def test_stats_failure_does_not_override_success_result():
    class _ExplodingStats(ToolStatsCollector):
        def record(self, name: str, success: bool, elapsed: float) -> None:
            raise RuntimeError("stats failed")

    tool = _CountingSuccessTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, _ExplodingStats(), ExecutionHooks())

    result = await executor.execute("param_tool", {"count": 1})

    assert tool.calls == 1
    assert result.success is True
    assert result.content == "accepted"


@pytest.mark.asyncio
async def test_audit_timeout_does_not_delay_or_override_success_result():
    class _HangingAuditor(ToolAuditor):
        async def record(self, **kwargs) -> None:
            await asyncio.sleep(1)

    tool = _CountingSuccessTool()
    service = ToolService(
        auditor=_HangingAuditor(),
        tool_observation_timeout=0.01,
    )
    service.register(tool)

    result = await asyncio.wait_for(
        service.execute("param_tool", {"count": 1}),
        timeout=0.2,
    )

    assert tool.calls == 1
    assert result.success is True


@pytest.mark.asyncio
async def test_audit_skip_notice_failure_does_not_override_tool_result():
    """审计因观察预算耗尽被跳过时，其告警抛错不得改写工具结果（G0-6）。

    预算耗尽只能由运行期衰减得到（observation_timeout 被校验为有限正数），
    故此处固定 _record_stats 的返回值为 0 来构造该状态。
    """
    tool = _CountingSuccessTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, ToolStatsCollector(), ExecutionHooks())
    executor._record_stats = lambda *args, **kwargs: 0.0

    with exploding_handler("app.tools.executor") as handler:
        result = await executor.execute("param_tool", {"count": 1})

    assert tool.calls == 1
    assert result.success is True
    assert result.content == "accepted"
    assert handler.calls >= 1


@pytest.mark.asyncio
async def test_observation_notice_failure_does_not_override_success_result():
    """观测超预算告警抛错时成功结果必须保留。

    该告警写在 _observe 的 except 子句内，处在 supervisor.wait 的保护区间之外。
    """

    class _HangingAuditor(ToolAuditor):
        async def record(self, **kwargs) -> None:
            await asyncio.sleep(1)

    tool = _CountingSuccessTool()
    service = ToolService(
        auditor=_HangingAuditor(),
        tool_observation_timeout=0.01,
    )
    service.register(tool)

    with exploding_handler("app.tools.executor") as handler:
        result = await asyncio.wait_for(service.execute("param_tool", {"count": 1}), timeout=0.5)

    assert tool.calls == 1
    assert result.success is True
    assert handler.calls >= 1


@pytest.mark.asyncio
async def test_stats_notice_failure_does_not_override_success_result():
    """统计降级告警抛错时成功结果必须保留（告警同在工具结果返回路径上）。"""

    class _ExplodingStats(ToolStatsCollector):
        def record(self, name: str, success: bool, elapsed: float) -> None:
            raise RuntimeError("stats failed")

    tool = _CountingSuccessTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, _ExplodingStats(), ExecutionHooks())

    with exploding_handler("app.tools.executor") as handler:
        result = await executor.execute("param_tool", {"count": 1})

    assert tool.calls == 1
    assert result.success is True
    assert handler.calls >= 1


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


class _TimeoutAfterBusinessFailTool(_ParamTool):
    """第 1 次返回业务失败，第 2 次起 sleep 触发超时（验证最终归因 = 最近失败）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return ToolResult(success=False, content="", error="业务失败")
        await asyncio.sleep(0.2)
        return ToolResult(success=True, content="ok")

    def can_retry(self, result_or_error: ToolResult | BaseException) -> bool:
        return True


@pytest.mark.asyncio
async def test_retry_final_timeout_overrides_earlier_business_failure():
    """业务失败后超时 → 全败归因最近失败（TIMEOUT 优先于更早业务失败，防归因错位）。"""
    service = ToolService(tool_max_retries=2, tool_timeout=0.05)
    service.register(_TimeoutAfterBusinessFailTool())

    result = await service.execute("param_tool", {"count": 1}, retry_delay=0)

    assert result.success is False
    assert result.error_code == ErrorCode.TIMEOUT
    assert "超时" in result.error
    assert result.retry_count == 2


class _ExplodeAfterBusinessFailTool(_ParamTool):
    """第 1 次返回业务失败，第 2 次起抛未捕获异常。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs) -> ToolResult:
        self.calls += 1
        if self.calls == 1:
            return ToolResult(success=False, content="", error="业务失败")
        raise RuntimeError("boom")

    def can_retry(self, result_or_error: ToolResult | BaseException) -> bool:
        return True


@pytest.mark.asyncio
async def test_retry_final_exception_overrides_earlier_business_failure():
    """业务失败后异常 → 全败归因最近失败（UNKNOWN 优先于更早业务失败）。"""
    service = ToolService(tool_max_retries=2)
    service.register(_ExplodeAfterBusinessFailTool())

    result = await service.execute("param_tool", {"count": 1}, retry_delay=0)

    assert result.success is False
    assert result.error_code == ErrorCode.UNKNOWN
    assert "boom" in result.error
    assert result.retry_count == 2
