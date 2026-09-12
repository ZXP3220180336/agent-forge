# LLM-044 执行控制未贯穿单次 generate/整流内部（取消/期限后仍发真实 SDK 请求）

> 发现：2026-09-08 ｜ 状态：✅ 已修复 ｜ 模块：llm_service / execution_control / retry / reservation（经执行控制外置）/ streaming_rectifier / structured / react + shared exceptions
> 关联：E [LLM-043](2026-09-08-structured-cancel-deadline.md) 覆盖边界修正；ADR [request-context-budget](../../../adr/integration/llm/2026-09-06-request-context-budget.md) Decision 8；后置修正见 [LLM-045](2026-09-09-execution-control-late-result-drop.md)（abort 判赢后迟回值接管）

## 发现

E（LLM-043）检查点只放 `structured._call_generate` 的 `await generate()` 之前一次，声称覆盖"所有真实请求"不成立：非流式 `generate()` 无 `cancel_event`/`deadline` → `_CallContext.cancel_event=None` → `_budget_guarded_call` reserve 后复查恒不命中、无 deadline 字段——generate 内部 retry 重试/fallback、整流/续接退避、reserve 排队、流 chunk 读取在取消/期限后仍发真实 SDK 请求。用户以真实 LLMService+RetryHandler+模拟 SDK 复现：首次 create 期间取消并耗尽期限 → 第二次 SDK 请求仍发出 → 返回"成功"。流式仅靠 react 外层 `asyncio.timeout` 有两点不足：`async_generate` 被直接调用时无总期限；cancel_event 离散检查，卡在 reserve/首包/chunk 等待时须等当前 await 返回才察觉。

## 分析

1. 信号到不了每次真实 attempt：`generate()` 未传 cancel → ctx 复查形同虚设；`_budget_guarded_call` 无 deadline。
2. 等待（reserve 排队 / retry 指数退避 / 整流与续接退避 / 首包与 chunk idle）不可被业务 cancel/deadline 中断——取消后仍白等且可能发下笔。
3. 三类等待分散（retry / StreamingRectifier / ReservationLimiter），无统一可中断等待原语。
4. 终止语义依赖内置 `TimeoutError`（会被 classify RETRYABLE 当网络超时重试/整流/续接）。
5. 私有信号若从 Facade 冒泡，Domain 将依赖 integration 私有异常（违反依赖方向）。

## 修复

统一"主副、流式/非流式共享准入与终止"，覆盖等待过程：

1. **终止信号双层**：integration 私有 `_ExecutionAbort` + `_StreamCancel`（升格继承）+ `_DeadlineExceeded`；shared 领域出口 `LLMCancelledError` / `LLMDeadlineExceededError`（NonRetryableError，携 usage）。Facade 边界（generate/async_generate）把私有翻译为 shared——Domain 不依赖 integration 私有；react 主循环收编（CANCELLED/TIMEOUT，不落 UNKNOWN/LLM_FAILED）。
2. **`_budget_guarded_call` 三检查点 + create_started 状态机**：入口检查 → 预算 → reserve 受执行控制（可中断排队，cancel task + await 内部 R5 退款回收）→ reserve 后复查（create 未启动，cancel 全退、竞态不丢返回值）→ create 受执行控制。**create 调度后取消/期限/外层硬取消一律 `settle(None)` 保守**（请求可能已达 provider，防配额虚增→429）；自然传输异常维持 cancel（重试语义）。
3. **`generate`/`async_generate` 加可选 `cancel_event`/`deadline` → `_CallContext.deadline`**；generate 检查点 3（结算后返回前，携 usage）；ReAct 双通道透传（`_llm_round_non_streaming` → generate 亦透传）。
4. **统一等待原语**（`execution_control.py`）：`wait_with_execution_control`（直接抛类型化信号）+ `await_with_execution_control`（收协程工厂防 never-awaited，取消后 await 清理）。覆盖 retry 退避、整流/续接退避、reserve 排队、fallback/续接每次 create 前。
5. **整流读取期**：`rectified_stream` 收 deadline；attempt 入口/迭代中断统一 deadline 终止（不整流/不续接）；`_drain` **四方竞争**（anext × cancel × deadline × idle，确定性判定序：吸收 chunk → 取消 → 期限 → 正常 → idle 传输超时），取消即时响应不再等当前 await；中断清理 cancel+await task 并关闭未读完的 provider 流；按已获 usage settle 或 settle(None)，避免 finally 双结算。
6. **structured**：`_call_generate` 透传 generate + `except` shared 终止累计 usage 后**继续 raise**；`extract` 最外层收敛 return None——整条降级链只终止一次，不空转 JSON mode/回喂/扩容级。
7. **实现审查补强**：流式首次 create 的 `retry.execute` 与半流续接链完整透传 deadline；chunk/终止同时完成时先写入 usage；ReAct 在终止异常收尾前合并异常携带的 usage；结构化入口已终止时抛类型化信号，避免逐级空转。

熔断口径：取消/期限本身不计熔断失败；终止前已实际发生的主网络可恢复故障仍保留一次失败记账（`saw_retryable_failure`）。reserve 排队响应 cancel/deadline——修订 ADR Decision 8「限流排队不提前打断」约定。

## 验证

- LLM-044 核心锁定测试（`test_llm_request_budget.py`）：P1 复现——retry 首次 create 超时后退避期间取消 → create 恰 1、上抛 `LLMCancelledError`；create 在途 deadline → `settle(None)` 非 cancel。
- 各层既有回归：llm_service/llm_request_budget/stream_rectify/streaming_rectifier/generate_structured/retry/reflection/planner/react 双通道/exceptions 全绿；全量 **878 passed**；verify_alignment 通过。
- 既有 stub 适配新可选参数（generate/async_generate `cancel_event`/`deadline`）。

## 教训

- **执行控制的检查点必须落在"每次真实 SDK attempt 的共用入口 + 每次等待"，而非编排层/结构化链层**：E 把检查放 `_call_generate` 门口一次，generate 内部 retry/fallback/reserve 仍是黑盒（与 REASON-001、LLM-043 同一教训：护栏锚定真实调用点）。
- **"请求是否已开始"决定结算方式**：create 调度后请求可能已达 provider——终止须 `settle(None)` 保守而非 cancel 全额退；不能靠"内部 deadline 总比外层 timeout 先触发"的调度假设，需显式 `create_started` 状态。
- **内置 `TimeoutError` 是执行终止语义、不是网络超时**：整体期限不得以它表现（classify RETRYABLE 会重试/整流/续接）；终止用类型化私有信号并在 Facade 翻译 shared 领域出口（依赖方向）。
