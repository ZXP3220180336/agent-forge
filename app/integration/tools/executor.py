"""工具执行器：共享准入 + 重试 + 超时 + 校验 + 截断 + 审计 + 统计 + 钩子。"""

import asyncio
import copy
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from app.domain.ports.tool_execution import (
    ToolCallContext,
    ToolCleanupState,
    ToolEffectState,
    ToolExecutionState,
    ToolFact,
    ToolFactSink,
)
from app.domain.ports.tool_gateway import ErrorCode, ToolResult
from app.integration.tools.admission import ToolAdmission
from app.integration.tools.base import BaseTool
from app.integration.tools.execution import (
    ToolAttemptHandle,
    ToolAttemptTimeoutError,
    ToolEffectClass,
    ToolExecutionSettings,
    ToolExecutionSupervisor,
    ToolShutdownIncompleteError,
    check_abort,
    is_execution_enabled,
    read_execution_spec,
    wait_for_abort,
)
from app.integration.tools.hooks import ExecutionHooks
from app.integration.tools.registry import ToolRegistry
from app.integration.tools.result_processor import ResultProcessor
from app.integration.tools.security import (
    ApprovalGate,
    AutoApprovalGate,
    RiskLevel,
    ToolAuditor,
)
from app.integration.tools.stats import ToolStatsCollector
from app.integration.tools.validator import ParameterValidator
from app.platform.observability.logger import get_logger
from app.shared.exceptions import (
    ToolCancelledError,
    ToolDeadlineExceededError,
    ToolRunStoppedError,
)
from app.shared.observation import isolate_observation

logger = get_logger("tools.executor")

# 适配器返回值必须满足的字段契约：(字段名, 期望类型, 是否允许 None)。
# 范围是「执行器会读取或转交领域」的字段；metadata 无消费方、execution_time 与
# retry_count 由执行器覆写，都不在此表内。见 ToolExecutor._invalid_result_reason。
_RESULT_FIELD_CONTRACT: tuple[tuple[str, type, bool], ...] = (
    ("success", bool, False),
    ("content", str, False),
    ("error", str, True),
    ("error_code", ErrorCode, True),
    ("effect_state", ToolEffectState, False),
)


class ToolExecutor:
    """执行编排：在共享准入内运行工具（重试/超时/校验/截断/审计），记录统计并触发钩子。

    依赖注入 registry（找工具）/ stats（统计记录）/ hooks（成功通知）/
    validator（参数校验）/ result_processor（结果截断）/ auditor（审计留痕）。
    是原 ToolService 执行逻辑的独立组件。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        stats: ToolStatsCollector,
        hooks: ExecutionHooks,
        *,
        validator: ParameterValidator | None = None,
        result_processor: ResultProcessor | None = None,
        auditor: ToolAuditor | None = None,
        approval_gate: ApprovalGate | None = None,
        max_concurrent_tools: int = 3,
        admission: ToolAdmission | None = None,
        tool_timeout: int = 30,
        tool_max_retries: int = 3,
        observation_timeout: float = 0.2,
        execution_settings: ToolExecutionSettings | None = None,
    ) -> None:
        self._registry = registry
        self._stats = stats
        self._hooks = hooks
        self._validator = validator or ParameterValidator()
        self._result_processor = result_processor or ResultProcessor()
        self._auditor = auditor or ToolAuditor()
        self._approval_gate = approval_gate or AutoApprovalGate()
        self._tool_timeout = tool_timeout
        self._tool_max_retries = tool_max_retries
        settings = execution_settings or ToolExecutionSettings(observation_timeout_seconds=observation_timeout)
        self._observation_timeout = settings.observation_timeout_seconds
        # 调用准备、审批及退避期间也固定实例；真实后台工作另外由 Supervisor 保护。
        self._active_tools: dict[BaseTool, int] = {}
        # 直接构造 Executor 时退化为同限额准入；生产装配注入跨运行共享的准入器。
        # 判定用 is None 而非真值：准入器是有状态对象，将来实现 __len__/__bool__ 也不会被误判为空。
        self._admission = (
            ToolAdmission(global_limit=max_concurrent_tools, per_run_limit=max_concurrent_tools)
            if admission is None
            else admission
        )
        self._supervisor = ToolExecutionSupervisor(settings=settings, max_workers=self._admission.global_limit)
        self._active_calls: set[ToolCallContext] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        # per-tool 锁：concurrency_safe=False 的工具同实例内串行化
        self._tool_locks: dict[str, asyncio.Lock] = {}
        # Integration 先接管事实；已交付且已完成清理的快照可释放。

        # 未决执行、关闭的 sink 或交付失败仍由 Integration 持有。
        self._latest_facts: dict[tuple[str, str | None], ToolFact] = {}

    check_abort = staticmethod(check_abort)

    def is_tool_active(self, tool: BaseTool) -> bool:
        """准备/退避中的调用或尚未真实完成的 attempt 都禁止卸载实例。"""
        return self._active_tools.get(tool, 0) > 0 or self._supervisor.owns(tool)

    def _publish_fact(
        self,
        call: ToolCallContext,
        facts: ToolFactSink,
        *,
        revision: int,
        execution_state: ToolExecutionState,
        effect_state: ToolEffectState,
        cleanup_state: ToolCleanupState,
        attempt_id: str | None = None,
        result: ToolResult | None = None,
    ) -> bool:
        """先接管专有快照，再同步通知 Domain；sink 异常按编程错误传播。"""
        owned = ToolFact(
            operation_id=call.operation_id,
            attempt_id=attempt_id,
            run_id=call.run_id,
            batch_id=call.batch_id,
            tool_call_id=call.tool_call_id,
            revision=revision,
            execution_state=execution_state,
            effect_state=effect_state,
            cleanup_state=cleanup_state,
            result=copy.deepcopy(result),
        )
        # 先复制并保存 Integration 自己拥有的事实
        self._latest_facts[(call.operation_id, attempt_id)] = owned
        try:
            # 再把副本交给 Domain 的 ToolFactSink
            acknowledged = facts.record(copy.deepcopy(owned))
        except Exception:
            # 事实入口失效后，Integration 仍保留本地快照，避免丢失已发生的执行结果。
            # 事实入口失效后，不允许同一 run 再启动业务调用；原异常保留给上层诊断。
            call.run_stop.set()
            raise
        if (
            # Domain 已接管该事实, 执行、清理都处于终局状态，允许释放 _latest_facts 中的本地缓存
            acknowledged is True
            and execution_state
            in {
                ToolExecutionState.NOT_STARTED,
                ToolExecutionState.SUCCEEDED,
                ToolExecutionState.FAILED,
            }
            and cleanup_state
            in {
                ToolCleanupState.COMPLETE,
                ToolCleanupState.NOT_NEEDED,
            }
        ):
            self._latest_facts.pop((call.operation_id, attempt_id), None)
        return acknowledged is True

    def begin_call(self, call: ToolCallContext, facts: ToolFactSink) -> None:
        """刷新和审批前预登记；同版本重复交付由事实入口幂等接管。"""
        self._publish_fact(
            call,
            facts,
            revision=0,
            execution_state=ToolExecutionState.NOT_STARTED,
            effect_state=ToolEffectState.NONE,
            cleanup_state=ToolCleanupState.NOT_NEEDED,
        )

    async def execute(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
        *,
        call: ToolCallContext,
        facts: ToolFactSink,
        pinned_tool: BaseTool | None = None,
        admission_deadline: float | None = None,
    ) -> ToolResult:
        """固定实例，准备调用；每次真实尝试分别准入，最后接管结果并复查控制。"""
        self.begin_call(call, facts)
        self.check_abort(call)
        tool = pinned_tool if pinned_tool is not None else self._registry.get(name)
        self._active_calls.add(call)
        self._idle.clear()
        if tool is not None:
            self._active_tools[tool] = self._active_tools.get(tool, 0) + 1
        try:
            result, execution_state, cleanup_state = await self._prepare_call(
                name,
                parameters,
                timeout,
                max_retries,
                retry_delay,
                call=call,
                facts=facts,
                tool=tool,
                admission_deadline=admission_deadline,
            )
            # 已执行的 attempt 在其结果回调中同步更新操作事实；这里不让展示结果或
            # 本地 TIMEOUT 覆盖清理期间迟回的真实成功。未执行的拒绝才在此发布。
            if result.retry_count == 0 and cleanup_state != ToolCleanupState.TRANSFERRED:
                self._publish_fact(
                    call,
                    facts,
                    revision=1,
                    execution_state=execution_state,
                    effect_state=ToolEffectState.NONE,
                    cleanup_state=cleanup_state,
                    result=result,
                )
            self.check_abort(call)
            return result
        finally:
            self._active_calls.discard(call)
            if not self._active_calls:
                self._idle.set()
            if tool is not None:
                remaining = self._active_tools[tool] - 1
                if remaining:
                    self._active_tools[tool] = remaining
                else:
                    self._active_tools.pop(tool, None)

    async def close(self) -> None:
        """先撤回排队，再有界排空真实工作；未排空抛错，禁止提前关闭依赖。"""
        end = time.monotonic() + self._supervisor.settings.shutdown_timeout_seconds
        await self._admission.close()
        for call in self._active_calls:
            call.run_stop.set()
        await self._supervisor.close()
        idle = asyncio.create_task(self._idle.wait())
        try:
            await asyncio.wait({idle}, timeout=max(0.0, end - time.monotonic()))
            if not self._idle.is_set():
                raise ToolShutdownIncompleteError("仍有准备或退避中的工具调用")
        finally:
            idle.cancel()
            await asyncio.gather(idle, return_exceptions=True)

    async def _prepare_call(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
        *,
        call: ToolCallContext,
        facts: ToolFactSink,
        tool: BaseTool | None,
        admission_deadline: float | None,
    ) -> tuple[ToolResult, ToolExecutionState, ToolCleanupState]:
        """准备固定实例与合法参数；真实尝试和审批各自受控准入。"""
        started_at = time.monotonic()

        # 首轮准入预算从 Facade 入口起算，刷新已耗尽时不再开始审批。
        if admission_deadline is not None and time.monotonic() >= admission_deadline:
            result = ToolResult(False, "", error="工具准入等待超时", error_code=ErrorCode.CAPACITY_EXCEEDED)
            await self._audit(tool, parameters, result, started_at=started_at, tool_name=name, call=call)
            return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED

        # 1. 查找工具
        if not tool:
            result = ToolResult(
                success=False,
                content="",
                error=f"工具 '{name}' 未注册",
                error_code=ErrorCode.NOT_REGISTERED,
            )
            await self._audit(None, parameters, result, started_at=started_at, tool_name=name, call=call)
            return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED

        # 2. 解析执行参数：调用方显式 > 工具自声明 > 全局配置（max_retries 仅调用方 / 全局两档）
        if timeout is None:
            timeout = tool.timeout if tool.timeout is not None else self._tool_timeout
        # 语义说明：max_retries 实际为「最大执行次数」（range(max_retries)），重试 = 次数 - 1；
        # 参数名沿契约保留（避免 ToolGateway 契约变动），此处注明避免误解
        max_retries = max_retries if max_retries is not None else self._tool_max_retries
        # 至少执行一次：max_retries=0（或全局配 0）意为「不重试跑一次」，
        # clamp 到 1 避免 range(0) 零次循环产生「未执行」的静默空失败
        max_retries = max(max_retries, 1)

        # 3. 解析参数（str → dict），并统一校验为「字符串键的 dict」
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except json.JSONDecodeError as e:
                result = ToolResult(
                    success=False,
                    content="",
                    error=self._result_processor.normalize_error(f"参数 JSON 解析失败: {e}"),
                    error_code=ErrorCode.JSON_PARSE,
                )
                await self._audit(tool, parameters, result, started_at=started_at, call=call)
                return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED
        # LLM 可能返回数组 / 标量 / null，或调用方误传非 str 键 dict——一律归 JSON_PARSE，
        # 否则后续 **parameters 抛 TypeError 逃逸编排层（违反「不让异常抛出」契约）
        if not isinstance(parameters, dict) or any(not isinstance(k, str) for k in parameters):
            result = ToolResult(
                success=False,
                content="",
                error=self._result_processor.normalize_error(
                    f"参数必须为 JSON 对象（字符串键），收到 {type(parameters).__name__}"
                ),
                error_code=ErrorCode.JSON_PARSE,
            )
            await self._audit(tool, parameters, result, started_at=started_at, call=call)
            return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED

        # 4. 参数前置校验（jsonschema 全量校验，错误可归因）
        issues = tool.validation_issues(**parameters)
        if issues:
            result = ToolResult(
                success=False,
                content="",
                error=self._result_processor.normalize_error(f"参数验证失败: {'; '.join(issues)}"),
                error_code=ErrorCode.VALIDATION,
            )
            await self._audit(tool, parameters, result, started_at=started_at, call=call)
            return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED

        if not is_execution_enabled(read_execution_spec(tool.describe_execution, parameters)):
            result = ToolResult(
                False,
                "",
                error="工具尚未启用：副作用、未知效果或强制审计操作需要持久保护",
                error_code=ErrorCode.REJECTED,
                effect_state=ToolEffectState.NONE,
            )
            await self._audit(tool, parameters, result, started_at=started_at, call=call)
            return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED

        # 审批本身也受控制；未完成的审批由宿主接管，不能无限等待。
        if tool.requires_approval:
            approved, approval_cleanup = await self._request_approval(
                tool,
                parameters,
                call,
                facts,
                admission_deadline,
            )
            if approved is not True:
                result = ToolResult(
                    False,
                    "",
                    error="工具调用被拒绝：等待人工审批" if approved is False else "工具审批准入容量不足",
                    error_code=ErrorCode.REJECTED if approved is False else ErrorCode.CAPACITY_EXCEEDED,
                )
                await self._audit(tool, parameters, result, started_at=started_at, call=call)
                return result, ToolExecutionState.NOT_STARTED, approval_cleanup

        # 审批完成后复查；per-tool 锁与 Permit 都属于单次真实尝试。
        self.check_abort(call)
        result, observation_remaining, execution_state, cleanup_state = await self._execute_with_retry(
            tool,
            parameters,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            call=call,
            facts=facts,
            admission_deadline=admission_deadline,
        )

        # 7. 审计（每次 execute 一条最终结果）
        observation_started_at = time.monotonic()
        await self._audit(
            tool,
            parameters,
            result,
            started_at=started_at,
            observation_timeout=observation_remaining,
            call=call,
        )
        observation_remaining = max(
            0.0,
            observation_remaining - (time.monotonic() - observation_started_at),
        )
        if result.success:
            await self._observe(
                lambda: self._hooks.run(name, parameters, result, timeout=observation_remaining),
                call=call,
                tool=tool,
                timeout=observation_remaining,
            )

        return result, execution_state, cleanup_state

    async def _request_approval(
        self,
        tool: BaseTool,
        parameters: dict[str, Any],
        call: ToolCallContext,
        facts: ToolFactSink,
        admission_deadline: float | None,
    ) -> tuple[bool | None, ToolCleanupState]:
        """审批消耗同一准入窗口；吞取消的审批仍占有真实容量和实例。"""
        permit = await self._admission.acquire(call, deadline=admission_deadline)
        if permit is None:
            return None, ToolCleanupState.NOT_NEEDED

        async def request(_handle: ToolAttemptHandle):
            return await self._approval_gate.request(tool.name, parameters)

        def transfer(_handle: ToolAttemptHandle) -> None:
            self._publish_fact(
                call,
                facts,
                revision=1,
                execution_state=ToolExecutionState.NOT_STARTED,
                effect_state=ToolEffectState.NONE,
                cleanup_state=ToolCleanupState.TRANSFERRED,
            )

        handle = None
        try:
            handle = self._supervisor.start(
                request,
                call=call,
                permit=permit,
                owner=tool,
                on_transfer=transfer,
            )
            if handle is None:
                return None, ToolCleanupState.NOT_NEEDED
        finally:
            if handle is None:
                permit.release()
        remaining = (
            self._admission.admission_timeout
            if admission_deadline is None
            else max(
                0.0,
                admission_deadline - time.monotonic(),
            )
        )
        try:
            approved = await self._supervisor.wait(handle, timeout=remaining)
            return approved, ToolCleanupState.COMPLETE
        except ToolAttemptTimeoutError:
            cleanup = ToolCleanupState.TRANSFERRED if handle.transferred else ToolCleanupState.COMPLETE
            return None, cleanup

    async def _audit(
        self,
        tool: BaseTool | None,
        parameters: dict[str, Any] | str,
        result: ToolResult,
        *,
        started_at: float,
        tool_name: str | None = None,
        observation_timeout: float | None = None,
        call: ToolCallContext,
    ) -> None:
        """统一审计出口：取工具元数据（未注册时传 None + 保留原始工具名）+ 最终 result。"""
        elapsed = time.monotonic() - started_at
        audit_params = parameters if isinstance(parameters, dict) else {"raw": str(parameters)[:500]}
        timeout = self._observation_timeout if observation_timeout is None else observation_timeout
        if timeout <= 0:
            # 该告警在 _observe 之前 return：它抛错会逃出工具执行，把跳过审计
            # 变成一次工具调用失败（G0-6 观测隔离）。
            isolate_observation(lambda: logger.warning("工具审计跳过：观察预算已耗尽"))
            return
        await self._observe(
            lambda: self._auditor.record(
                tool_name=tool.name if tool else (tool_name or "unknown"),
                risk_level=tool.risk_level if tool else RiskLevel.L0_READONLY,
                category=tool.category if tool else "unknown",
                success=result.success,
                elapsed=elapsed,
                parameters=audit_params,
                error=result.error,
                error_code=result.error_code,
                retry_count=result.retry_count,
                content_preview=result.content,
            ),
            call=call,
            tool=tool,
            timeout=timeout,
        )

    async def _observe(
        self,
        factory: Callable[[], Awaitable[None]],
        *,
        call: ToolCallContext,
        tool: BaseTool | None,
        timeout: float,
    ) -> None:
        """观测与业务共享真实容量；预算后移交而不等待吞取消的观察器。"""
        if timeout <= 0:
            return
        end = time.monotonic() + timeout
        # 必要收尾不再沿用已命中的业务终止信号，但不能取得新的清理宽限。
        observation = replace(call, cancel_events=(), run_stop=asyncio.Event(), deadline=end, cleanup_deadline=end)
        try:
            # 不占业务 Permit；仍在 start 前占用有界宿主跟踪条目，不能派生线程工作。
            # 这样排队拒绝也能留痕，观测占满条目时则明确跳过而不扩大后台容量。
            handle = self._supervisor.start(lambda _: factory(), call=observation, permit=None, owner=tool)
            if handle is not None:
                await self._supervisor.wait(handle, timeout=max(0.0, end - time.monotonic()))
        except Exception as error:  # noqa: BLE001 — 非关键观测不覆盖业务事实。
            # 本条告警在 except 子句内，处于 supervisor.wait 的保护区间之外：
            # 它抛错会逃出工具执行，把成功结果变成异常（G0-6 观测隔离）。
            # 先取出异常再交给隔离边界：except 绑定名在块结束时被删除，不能进闭包。
            reason = error
            isolate_observation(lambda: logger.warning("工具观测失败或超过预算（结果保持）: %s", reason))

    async def _execute_with_retry(
        self,
        tool: BaseTool,
        parameters: dict[str, Any],
        *,
        timeout: int,
        max_retries: int,
        retry_delay: float,
        call: ToolCallContext,
        facts: ToolFactSink,
        admission_deadline: float | None,
    ) -> tuple[ToolResult, float, ToolExecutionState, ToolCleanupState]:
        """次数 Owner；每轮独立准入，前轮仍在后台时绝不重放。"""
        observation_remaining = self._observation_timeout
        for attempt in range(max_retries):
            self.check_abort(call)
            result, failure, state, cleanup, retry_allowed = await self._run_attempt(
                tool,
                parameters,
                timeout=timeout,
                attempt=attempt,
                call=call,
                facts=facts,
                admission_deadline=admission_deadline if attempt == 0 else None,
            )
            if state != ToolExecutionState.NOT_STARTED:
                observation_remaining = self._record_stats(
                    tool.name,
                    success=result.success,
                    elapsed=result.execution_time,
                    observation_remaining=observation_remaining,
                )
            # 事实已接管才裁决下一步。终止不能进入展示回调或新尝试。
            self.check_abort(call)
            if result.success:
                return self._complete_call(tool, result), observation_remaining, state, cleanup
            if (
                attempt + 1 >= max_retries
                or not retry_allowed
                or cleanup != ToolCleanupState.COMPLETE
                or not self._can_retry(tool, failure)
            ):
                break
            # 到这里前一次真实工作已结束，Permit 与串行锁均已释放。
            await self._backoff(retry_delay * (2**attempt), call)
        return result, observation_remaining, state, cleanup

    async def _backoff(self, delay: float, call: ToolCallContext) -> None:
        """仅等待时间和控制信号；没有后台业务工作，退出时可直接回收等待者。"""
        self.check_abort(call)
        abort = asyncio.create_task(wait_for_abort(call))
        try:
            done, _ = await asyncio.wait({abort}, timeout=max(0.0, delay))
            if abort in done:
                await abort
            self.check_abort(call)
        finally:
            abort.cancel()
            await asyncio.gather(abort, return_exceptions=True)

    async def _run_attempt(
        self,
        tool: BaseTool,
        parameters: dict[str, Any],
        *,
        timeout: int,
        attempt: int,
        call: ToolCallContext,
        facts: ToolFactSink,
        admission_deadline: float | None,
    ) -> tuple[ToolResult, ToolResult | BaseException, ToolExecutionState, ToolCleanupState, bool]:
        """将 Permit 和真实工作一次性转交 Supervisor；回调只负责结果事实。"""
        spec = read_execution_spec(tool.describe_execution, parameters)
        if not is_execution_enabled(spec):
            result = ToolResult(
                False,
                "",
                error="工具尚未启用：副作用、未知效果或强制审计操作需要持久保护",
                error_code=ErrorCode.REJECTED,
                retry_count=attempt,
                effect_state=ToolEffectState.NONE,
            )
            return result, result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED, False
        permit = await self._admission.acquire(call, deadline=admission_deadline)
        if permit is None:
            result = ToolResult(
                False, "", error="工具调用排队容量已满或准入等待超时", error_code=ErrorCode.CAPACITY_EXCEEDED
            )
            result.retry_count = attempt
            return result, result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED, False

        attempt_id = uuid.uuid4().hex
        started_at = time.monotonic()
        started = False
        lock = None if tool.concurrency_safe else self._tool_lock(tool.name)
        lock_owned = False
        outcome = None

        def publish_running() -> None:
            self._publish_fact(
                call,
                facts,
                revision=0,
                attempt_id=attempt_id,
                execution_state=ToolExecutionState.RUNNING,
                effect_state=ToolEffectState.UNKNOWN,
                cleanup_state=ToolCleanupState.PENDING,
            )

        notify_start = publish_running

        async def invoke(handle: ToolAttemptHandle):
            nonlocal started, lock_owned, started_at, notify_start, spec
            if lock is not None:
                await lock.acquire()
                lock_owned = True
            handle.check_abort()
            # 排队和串行等待可跨越声明更新；真实调用前重新取得可信能力快照。
            spec = read_execution_spec(tool.describe_execution, parameters)
            if not is_execution_enabled(spec):
                notify_start = None
                return ToolResult(
                    False,
                    "",
                    error="工具尚未启用：副作用、未知效果或强制审计操作需要持久保护",
                    error_code=ErrorCode.REJECTED,
                    effect_state=ToolEffectState.NONE,
                )
            try:
                notify_start()
            finally:
                # 真实 invoke 在后台继续时不再经此闭包持有 Domain sink。
                notify_start = None
            started_at = time.monotonic()
            started = True
            return await tool.invoke(parameters, handle)

        def release_lock() -> None:
            # 此回调只持有锁；移交后不通过闭包保留 Domain collector。
            if lock is not None and lock_owned:
                lock.release()

        def take_result(handle: ToolAttemptHandle) -> None:
            nonlocal outcome
            outcome = self._attempt_result(handle, started=started, timeout=timeout)
            result, _, state, cleanup, _ = outcome
            if spec.effect_class == ToolEffectClass.READ_ONLY:
                # 无副作用来自可信适配器声明，不能从 success 或 risk_level 推断。
                result.effect_state = ToolEffectState.NONE
            result.execution_time = round(time.monotonic() - started_at, 4)
            result.retry_count = attempt + 1 if started else attempt
            attempt_ack = self._publish_fact(
                call,
                facts,
                revision=1,
                attempt_id=attempt_id,
                execution_state=state,
                effect_state=result.effect_state,
                cleanup_state=cleanup,
                result=result,
            )
            operation_ack = self._publish_fact(
                call,
                facts,
                revision=attempt + 1,
                execution_state=state,
                effect_state=result.effect_state,
                cleanup_state=cleanup,
                result=result,
            )
            if not attempt_ack or not operation_ack:
                handle.retain_record()

        handle = None
        try:
            self.check_abort(call)
            handle = self._supervisor.start(
                invoke,
                call=call,
                permit=permit,
                owner=tool,
                on_complete=take_result,
                on_transfer=take_result,
                on_release=release_lock,
            )
            if handle is None:
                result = ToolResult(
                    False, "", error="工具接管容量已满或执行宿主已关闭", error_code=ErrorCode.CAPACITY_EXCEEDED
                )
                result.retry_count = attempt
                return result, result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED, False
        finally:
            # start 成功后只有 Supervisor 有权归还，外层退出不能提前退槽。
            if handle is None:
                permit.release()

        try:
            await self._supervisor.wait(handle, timeout=timeout)
        except ToolCancelledError, ToolDeadlineExceededError, ToolRunStoppedError, asyncio.CancelledError:
            # 完成或移交回调已经接管 attempt 与操作事实，控制信号保持原类型。
            raise
        except ToolAttemptTimeoutError:
            # 清理期间的迟回成功保留在事实中；本轮等待超时不能改报正常成功。
            assert outcome is not None
            actual, _, state, cleanup, _ = outcome
            result = ToolResult(
                False,
                "",
                error=f"工具执行超时（{timeout}秒）",
                error_code=ErrorCode.TIMEOUT,
                effect_state=actual.effect_state,
            )
            result.retry_count = attempt + 1 if started else attempt
            result.execution_time = round(time.monotonic() - started_at, 4)
            return result, result, ToolExecutionState.FAILED if handle.completed else state, cleanup, False
        except Exception:
            # 只有真实调用错误可以按工具失败返回；事实交付错误仍原样传播。
            if outcome is None or handle.callback_error is not None:
                raise
        assert outcome is not None
        return outcome

    def _attempt_result(
        self,
        handle: ToolAttemptHandle,
        *,
        started: bool,
        timeout: int,
    ) -> tuple[ToolResult, ToolResult | BaseException, ToolExecutionState, ToolCleanupState, bool]:
        """解释真实调用出口；传输超时和本地未完成分别保留执行/清理事实。"""
        cleanup = ToolCleanupState.COMPLETE if handle.completed else ToolCleanupState.TRANSFERRED
        if not started:
            if handle.completed and handle.error is None and isinstance(handle.value, ToolResult):
                # 宿主已释放等待期间取得的 Permit/锁，业务未启动，无需业务清理。
                result = handle.value
                return result, result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED, False
            result = ToolResult(False, "", error="工具在调度前终止", effect_state=ToolEffectState.NONE)
            return result, handle.error or result, ToolExecutionState.NOT_STARTED, cleanup, False
        if not handle.completed or isinstance(handle.error, asyncio.CancelledError):
            result = ToolResult(False, "", error=f"工具执行中止或超时（{timeout}秒）", error_code=ErrorCode.TIMEOUT)
            state = ToolExecutionState.FAILED if handle.completed else ToolExecutionState.UNKNOWN
            return result, handle.error or result, state, cleanup, False
        if handle.error is not None:
            result = ToolResult(
                False,
                "",
                error=self._normalize_error(f"工具执行异常: {handle.error!s}"),
                error_code=ErrorCode.TIMEOUT if isinstance(handle.error, TimeoutError) else ErrorCode.UNKNOWN,
            )
            return result, handle.error, ToolExecutionState.FAILED, cleanup, True
        result = handle.value
        invalid_reason = self._invalid_result_reason(result)
        if invalid_reason is not None:
            result = ToolResult(False, "", error=f"工具返回了非法结果: {invalid_reason}", error_code=ErrorCode.UNKNOWN)
        state = ToolExecutionState.SUCCEEDED if result.success else ToolExecutionState.FAILED
        return result, result, state, cleanup, invalid_reason is None

    def _complete_call(self, tool: BaseTool, result: ToolResult) -> ToolResult:
        """原结果已归事实层所有；展示只处理副本，失败不能触发业务重放。"""
        try:
            processed = copy.deepcopy(result)
            self._result_processor.truncate_result(processed, max_length=tool.max_output_length)
            return processed
        except Exception as error:  # noqa: BLE001
            logger.warning("工具结果展示处理失败（保留原结果）: %s", error)
            return result

    @staticmethod
    def _invalid_result_reason(result: object) -> str | None:
        """返回值违反 `ToolResult` 契约时返回原因；合法返回 `None`。

        `BaseTool` 的类型标注约束不了动态加载的外部适配器。检查范围按**下游如何消费
        该字段**确定，不按“会不会抛异常”确定：

        - `content` 会被 ReAct 切片、`error_code` 会被取 `.value` → 越界即抛异常；
        - `success` 走真值判定 → 越界会把失败静默当成业务成功；
        - `error` 与 `effect_state` 会被转交事实与审计，必须是声明类型。

        `metadata` 是无消费方的扩展位，`execution_time` / `retry_count` 由执行器覆写，
        都不在边界内。越界值一律收敛为 `UNKNOWN` 失败，不做静默强转——强转会把适配器
        缺陷变成看起来正常的观测回喂给模型。

        字段契约用 `getattr` 逐项取值：静态检查按声明类型会把这些校验判为死代码，而
        边界要防的正是“运行时违反声明类型”。
        """
        if not isinstance(result, ToolResult):
            return f"类型 {type(result).__name__}"
        for field, expected, optional in _RESULT_FIELD_CONTRACT:
            value = getattr(result, field)
            if value is None and optional:
                continue
            if not isinstance(value, expected):
                return f"字段 {field} 为 {type(value).__name__}"
        return None

    @staticmethod
    def _can_retry(tool: BaseTool, failure: ToolResult | BaseException) -> bool:
        """读取适配器的安全声明；声明逻辑失败时按不可重试处理。"""
        try:
            return tool.can_retry(failure)
        except Exception as error:  # noqa: BLE001
            logger.warning("工具重试安全判断失败（停止重试）: %s", error)
            return False

    def _record_stats(
        self,
        name: str,
        *,
        success: bool,
        elapsed: float,
        observation_remaining: float,
    ) -> float:
        """统计属于非关键观测，失败不能覆盖真实工具结果。"""
        observation_started_at = time.monotonic()
        try:
            self._stats.record(name, success=success, elapsed=elapsed)
        except Exception as error:  # noqa: BLE001
            # 统计属非关键观测：告警失败不得改写真实工具结果（G0-6 观测隔离）。
            # 先取出异常再交给隔离边界：except 绑定名在块结束时被删除，不能进闭包。
            reason = error
            isolate_observation(lambda: logger.warning("工具统计记录失败（不影响执行）: %s", reason))
        return max(
            0.0,
            observation_remaining - (time.monotonic() - observation_started_at),
        )

    def _normalize_error(self, error: str) -> str:
        """错误展示处理失败时保留原始归因，不覆盖执行事实。"""
        try:
            return self._result_processor.normalize_error(error)
        except Exception as processing_error:  # noqa: BLE001
            logger.warning("工具错误展示处理失败（保留原错误）: %s", processing_error)
            return error

    def _tool_lock(self, name: str) -> asyncio.Lock:
        """惰性 per-tool 锁（asyncio.Lock 3.10+ 不绑定事件循环，惰性创建安全）。"""
        if name not in self._tool_locks:
            self._tool_locks[name] = asyncio.Lock()
        return self._tool_locks[name]

    def prune_tool_lock(self, name: str) -> None:
        """注销工具时清理 per-tool 锁条目（由 ToolService.unregister 调用）。

        跳过仍在持有的锁：外部工具重载时在飞 execute 持锁，先 pop 会导致
        新实例惰性建新锁、与旧实例并发（破坏串行化）；跳过则新实例复用同一把锁。
        """
        lock = self._tool_locks.get(name)
        if lock is not None and not lock.locked():
            self._tool_locks.pop(name, None)
