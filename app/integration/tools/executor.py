"""工具执行器：信号量 + 重试 + 超时 + 校验 + 截断 + 审计 + 统计 + 钩子。"""

import asyncio
import copy
import json
import time
import uuid
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
from app.integration.tools.base import BaseTool
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

logger = get_logger("tools.executor")


class ToolExecutor:
    """执行编排：在信号量内运行工具（重试/超时/校验/截断/审计），记录统计并触发钩子。

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
        tool_timeout: int = 30,
        tool_max_retries: int = 3,
        observation_timeout: float = 0.2,
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
        self._observation_timeout = observation_timeout
        # 信号量构造期创建：asyncio 原语 3.10+ 惰性绑定事件循环，构造期创建仅需保证实例就绪
        self._tool_semaphore = asyncio.Semaphore(max_concurrent_tools)
        # per-tool 锁：concurrency_safe=False 的工具同实例内串行化
        self._tool_locks: dict[str, asyncio.Lock] = {}
        # Integration 先接管事实；已交付且已完成清理的快照可释放。

        # 未决执行、关闭的 sink 或交付失败仍由 Integration 持有。
        self._latest_facts: dict[tuple[str, str | None], ToolFact] = {}

    @staticmethod
    def check_abort(call: ToolCallContext) -> None:
        """按冻结契约顺序检查取消、绝对期限与 run 关闭。"""
        if any(event.is_set() for event in call.cancel_events):
            raise ToolCancelledError(
                "工具调用已取消",
                run_id=call.run_id,
                operation_id=call.operation_id,
            )
        if call.deadline is not None and time.monotonic() >= call.deadline:
            raise ToolDeadlineExceededError(
                "工具调用期限已到",
                run_id=call.run_id,
                operation_id=call.operation_id,
            )
        if call.run_stop.is_set():
            raise ToolRunStoppedError(
                "所属运行已停止新的工具调用",
                run_id=call.run_id,
                operation_id=call.operation_id,
            )

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
    ) -> None:
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
    ) -> ToolResult:
        """执行工具（信号量最外层，包裹含重试退避的完整流程）。

        async with 天然保证异常/取消时释放信号量，不会挂死占坑。
        """

        # 预登记一次事实，确保至少有一条 NOT_STARTED 记录，避免上层 run 只收到空事实。
        self._publish_fact(
            call,
            facts,
            revision=0,
            execution_state=ToolExecutionState.NOT_STARTED,
            effect_state=ToolEffectState.NONE,
            cleanup_state=ToolCleanupState.NOT_NEEDED,
        )

        # 入口终止：调用前检查取消 / 绝对期限 / run 停止，“快速拒绝”，
        # 可以避免信号量被占用后的取消态依然排队等待，不会进入信号量排队，也不会占用并发槽。
        self.check_abort(call)

        # 如果调用已经进入信号量等待，取消信号到达后，
        # asyncio.Semaphore.acquire() 本身不会立即被 cancel_event 唤醒。
        # 任务仍可能要等到其他调用释放信号量然后取得并发许可后，才能执行第二次检查。
        # 【即取消后不会继续执行真实工具，但不保证排队等待会立即结束。】
        # 这也是为什么进入信号量排队之前要增加“快速拒绝”的原因
        async with self._tool_semaphore:
            # 排队竞态：取得并发许可后再次检查取消 / 绝对期限 / run 停止，处理排队期间的竞态，
            # 避免信号量排队期间被取消后仍进入执行。
            self.check_abort(call)

            # 真实执行（含重试循环、审计、统计、钩子）
            result, execution_state, cleanup_state = await self._execute_impl(
                name,
                parameters,
                timeout=timeout,
                max_retries=max_retries,
                retry_delay=retry_delay,
                call=call,
                facts=facts,
            )

        # 真实调用返回后先发布事实，再复查控制；取消不会抹除已发生结果。
        effect_state = (
            ToolEffectState.NONE if execution_state == ToolExecutionState.NOT_STARTED else result.effect_state
        )

        # 发布最终事实
        self._publish_fact(
            call,
            facts,
            revision=1,
            execution_state=execution_state,
            effect_state=effect_state,
            cleanup_state=cleanup_state,
            result=result,
        )

        # 调用后终态：复查取消 / 绝对期限 / run 停止，避免在信号量释放后才发现调用已被取消。
        self.check_abort(call)

        return result

    async def _execute_impl(
        self,
        name: str,
        parameters: dict[str, Any] | str,
        timeout: int | None = None,
        max_retries: int | None = None,
        retry_delay: float = 1.0,
        *,
        call: ToolCallContext,
        facts: ToolFactSink,
    ) -> tuple[ToolResult, ToolExecutionState, ToolCleanupState]:
        """执行工具（带参数校验、自动重试、结果截断、审计留痕），在信号量保护内调用。"""
        started_at = time.monotonic()

        # 1. 查找工具
        tool = self._registry.get(name)
        if not tool:
            result = ToolResult(
                success=False,
                content="",
                error=f"工具 '{name}' 未注册",
                error_code=ErrorCode.NOT_REGISTERED,
            )
            await self._audit(None, parameters, result, started_at=started_at, tool_name=name)
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
                await self._audit(tool, parameters, result, started_at=started_at)
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
            await self._audit(tool, parameters, result, started_at=started_at)
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
            await self._audit(tool, parameters, result, started_at=started_at)
            return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED

        # 5. 人工审批拦截（requires_approval 工具需 ApprovalGate 确认，默认 AutoApprovalGate 放行）
        if tool.requires_approval and not await self._approval_gate.request(name, parameters):
            result = ToolResult(
                success=False,
                content="",
                error="工具调用被拒绝：等待人工审批",
                error_code=ErrorCode.REJECTED,
            )
            await self._audit(tool, parameters, result, started_at=started_at)
            return result, ToolExecutionState.NOT_STARTED, ToolCleanupState.NOT_NEEDED

        # 6. 执行（重试循环）；concurrency_safe=False 时 per-tool 锁串行化
        if tool.concurrency_safe:
            (result, observation_remaining, execution_state, cleanup_state) = await self._execute_with_retry(
                tool,
                parameters,
                timeout=timeout,
                max_retries=max_retries,
                retry_delay=retry_delay,
                call=call,
                facts=facts,
            )
        else:
            async with self._tool_lock(name):
                (result, observation_remaining, execution_state, cleanup_state) = await self._execute_with_retry(
                    tool,
                    parameters,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                    call=call,
                    facts=facts,
                )

        # 7. 审计（每次 execute 一条最终结果）
        observation_started_at = time.monotonic()
        await self._audit(
            tool,
            parameters,
            result,
            started_at=started_at,
            observation_timeout=observation_remaining,
        )
        observation_remaining = max(
            0.0,
            observation_remaining - (time.monotonic() - observation_started_at),
        )
        if result.success:
            await self._hooks.run(
                name,
                parameters,
                result,
                timeout=observation_remaining,
            )

        return result, execution_state, cleanup_state

    async def _audit(
        self,
        tool: BaseTool | None,
        parameters: dict[str, Any] | str,
        result: ToolResult,
        *,
        started_at: float,
        tool_name: str | None = None,
        observation_timeout: float | None = None,
    ) -> None:
        """统一审计出口：取工具元数据（未注册时传 None + 保留原始工具名）+ 最终 result。"""
        elapsed = time.monotonic() - started_at
        audit_params = parameters if isinstance(parameters, dict) else {"raw": str(parameters)[:500]}
        timeout = self._observation_timeout if observation_timeout is None else observation_timeout
        if timeout <= 0:
            logger.warning("工具审计跳过：观察预算已耗尽")
            return
        try:
            await asyncio.wait_for(
                self._auditor.record(
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
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001 — 审计失败不阻断工具执行
            logger.warning("工具审计失败（不影响执行）: %s", e)

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
    ) -> tuple[ToolResult, float, ToolExecutionState, ToolCleanupState]:
        """重试循环：超时保护 + 渐进式退避 + 成功截断 + 统计 + 钩子。"""
        name = tool.name
        last_error: str | None = None
        last_error_code: ErrorCode | None = None
        last_result: ToolResult | None = None
        actual_retries = 0
        execution_state = ToolExecutionState.NOT_STARTED
        cleanup_state = ToolCleanupState.NOT_NEEDED
        observation_remaining = self._observation_timeout

        for attempt in range(max_retries):
            # 重试竞态：每轮循环前检查取消 / 绝对期限 / run 停止，
            # 避免在重试退避期间被取消后仍进入执行。
            self.check_abort(call)

            # 发布 RUNNING 事实
            attempt_id = uuid.uuid4().hex
            self._publish_fact(
                call,
                facts,
                revision=0,
                attempt_id=attempt_id,
                execution_state=ToolExecutionState.RUNNING,
                effect_state=ToolEffectState.UNKNOWN,
                cleanup_state=ToolCleanupState.PENDING,
            )

            start_time = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    tool.execute(**parameters),
                    timeout=timeout,
                )
            except TimeoutError as error:
                # 超时语义：`wait_for` 超时取消的是执行协程；工具内部经 `asyncio.to_thread`
                # 包装的同步 SDK 调用（如 Tavily 搜索）**无法被取消**——线程池线程会继续运行至底层返回，
                # 超时后资源不立即释放。这是 `to_thread` + 超时的固有行为（非泄漏）。
                # 【“本地等待超时”不等于“远端工具已经停止”】
                execution_state = ToolExecutionState.UNKNOWN
                cleanup_state = ToolCleanupState.PENDING
                failure: ToolResult | BaseException = error
                last_error = self._normalize_error(f"工具执行超时（{timeout}秒）")
                last_error_code = ErrorCode.TIMEOUT
                # 覆盖上次业务失败结果：全败收尾统一归因「最近一次失败」（超时优先于更早的业务失败）
                last_result = None
                elapsed = time.monotonic() - start_time
                observation_remaining = self._record_stats(
                    name,
                    success=False,
                    elapsed=elapsed,
                    observation_remaining=observation_remaining,
                )

                self._publish_fact(
                    call,
                    facts,
                    revision=1,
                    attempt_id=attempt_id,
                    execution_state=ToolExecutionState.UNKNOWN,
                    effect_state=ToolEffectState.UNKNOWN,
                    cleanup_state=ToolCleanupState.PENDING,
                    result=ToolResult(
                        success=False,
                        content="",
                        error=last_error,
                        error_code=last_error_code,
                        retry_count=attempt + 1,
                    ),
                )

            except Exception as error:  # noqa: BLE001
                execution_state = ToolExecutionState.FAILED
                cleanup_state = ToolCleanupState.COMPLETE
                failure = error
                last_error = self._normalize_error(f"工具执行异常: {error!s}")
                last_error_code = ErrorCode.UNKNOWN
                # 同上：异常覆盖更早的业务失败，避免错误归因错位（审计 / 证据链按最终失败归类）
                last_result = None
                elapsed = time.monotonic() - start_time
                observation_remaining = self._record_stats(
                    name,
                    success=False,
                    elapsed=elapsed,
                    observation_remaining=observation_remaining,
                )

                self._publish_fact(
                    call,
                    facts,
                    revision=1,
                    attempt_id=attempt_id,
                    execution_state=ToolExecutionState.FAILED,
                    effect_state=ToolEffectState.UNKNOWN,
                    cleanup_state=ToolCleanupState.COMPLETE,
                    result=ToolResult(
                        success=False,
                        content="",
                        error=last_error,
                        error_code=last_error_code,
                        retry_count=attempt + 1,
                    ),
                )

            else:
                # execute 已返回，因此本地尝试已完成；业务错误码不反推执行阶段。
                # 远端效果独立采用 result.effect_state，仍可为 UNKNOWN/PARTIAL。
                execution_state = ToolExecutionState.SUCCEEDED if result.success else ToolExecutionState.FAILED
                cleanup_state = ToolCleanupState.COMPLETE
                # 真实调用已返回；以下处理不属于调用失败，异常不得触发工具重放。
                # 填充执行元数据：retry_count = 实际执行次数（第 1 次尝试 = 1，0 基索引 +1），
                # 成功 / 失败路径口径一致（全败路径用 actual_retries = 同一语义）
                elapsed = time.monotonic() - start_time
                result.execution_time = round(elapsed, 4)
                result.retry_count = attempt + 1
                self._publish_fact(
                    call,
                    facts,
                    revision=1,
                    attempt_id=attempt_id,
                    execution_state=(ToolExecutionState.SUCCEEDED if result.success else ToolExecutionState.FAILED),
                    effect_state=result.effect_state,
                    cleanup_state=ToolCleanupState.COMPLETE,
                    result=result,
                )

                if result.success:
                    # 原始结果已归调用层所有；展示处理在副本上执行，失败时保留原结果。
                    try:
                        processed_result = copy.deepcopy(result)
                        self._result_processor.truncate_result(processed_result, max_length=tool.max_output_length)
                    except Exception as error:  # noqa: BLE001
                        logger.warning("工具结果展示处理失败（保留原结果）: %s", error)
                        processed_result = result
                    observation_remaining = self._record_stats(
                        name,
                        success=True,
                        elapsed=elapsed,
                        observation_remaining=observation_remaining,
                    )
                    return (
                        processed_result,
                        observation_remaining,
                        execution_state,
                        cleanup_state,
                    )

                # 执行返回失败（如文件不存在）→ 记录错误与业务码，准备重试
                last_error = self._normalize_error(result.error or "工具执行失败")
                last_error_code = result.error_code  # 透传工具业务码（默认 None）
                last_result = result
                failure = result
                observation_remaining = self._record_stats(
                    name,
                    success=False,
                    elapsed=elapsed,
                    observation_remaining=observation_remaining,
                )

            actual_retries += 1

            # 仅在安全声明允许重试且未超出次数预算时才进入下一轮循环；否则直接 break。
            if attempt >= max_retries - 1 or not self._can_retry(tool, failure):
                break

            # 已同时满足安全声明和次数预算，才进入下一次真实执行。
            wait = retry_delay * (2**attempt)
            await asyncio.sleep(wait)

        # 所有重试均失败：retry_count = 实际执行次数（每轮循环 +1，与成功路径 attempt+1 口径一致）
        # last_result 仅保留「最近一次业务失败」；最后一次为超时 / 异常时回退到 last_error / last_error_code
        result = last_result or ToolResult(
            success=False,
            content="",
            error=last_error,
            error_code=last_error_code,
            retry_count=actual_retries,
        )
        result.retry_count = actual_retries
        if result.execution_time is None:
            result.execution_time = 0.0
        return result, observation_remaining, execution_state, cleanup_state

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
            logger.warning("工具统计记录失败（不影响执行）: %s", error)
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
