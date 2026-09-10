# LLM-043 generate_structured 降级链无取消/期限检查点（终止后仍空烧付费调用）

> 发现：2026-09-08 ｜ 状态：✅ 已修复 ｜ 模块：structured（`StructuredOutput`）/ llm_service / reflection / planner
> 关联：预算闸 Slice-1 [LLM-041](./2026-09-06-fallback-window-and-quota.md) 之后「所有真实请求取消/期限覆盖」的最后一环（todo §6-E / Slice 5）；ADR [request-context-budget](../../../adr/integration/llm/2026-09-06-request-context-budget.md) Decision 8 的 structured 兑现

## 发现

`StructuredOutput.extract` 三级降级链（JSON Schema → JSON mode → prompt 正则），每级内还有**截断扩 2 倍 token 重试 + Schema 校验回喂**——单次 `generate_structured` 最坏 ~9 次真实 SDK 请求。**整条链无任何取消/期限检查点**：

- `cancel_event` / `max_execution_time` 只被 reflection/planner 在「阶段入口」自查（当时为 `guard_exceeded`，现 `evaluate_guard`）；一旦进入 `_critique`/`_refine`/plan/replan/summarize 的 `generate_structured`，内部 1~9 次调用跑满为止——用户取消/总时长耗尽后仍继续付费调用。
- 全仓仅 `react.py` 一处 `asyncio.timeout`（包 react 主循环），reflection/planner 的 structured 调用不在任何硬超时内，全靠自查——缺口是真实的。

## 分析

1. **信号到不了 structured**：`LLMGateway.generate_structured` 端口与 `StructuredOutput.extract` 都无 `cancel_event`/`deadline` 参数；reflection/planner 持有两量（execute 形参 + `start_time`）却传不进。
2. **整体 deadline 若以 `asyncio.timeout` 注入会被当可恢复网络超时吞掉**：`_call_generate` 的 `except Exception` → `decide_downstream_error` 把内置 `TimeoutError` 归类 RETRYABLE → 返回 None 降级**下一级再调用**——todo §4 明文「整体 DeadlineExceeded 不得被当网络超时、必须保留执行终止语义」在此坐实（当前无激活源，未来策略边界加硬超时即触发）。
3. **usage 少计**：reflection/planner 的 `except AppError` 返回 `usage=None`，丢弃链内**已成功调用**的用量（拒答/熔断等上抛路径）——成本计量系统性低估。

## 修复

采用「信号下沉 + 检查点单点 + 终止与降级同出口」，不改 `generate_structured` 返回契约：

1. `generate_structured` / `StructuredOutput.extract` 增加可选 `cancel_event: asyncio.Event | None = None` 与 `deadline: float | None = None`（monotonic **绝对**时刻，调用方现算 `start_time + max_execution_time`；默认 None 向后兼容）。
2. 检查点只放 `_call_generate` 入口（三级初始/截断扩容/回喂/fallback 所有真实请求必经）：命中 `_should_abort` 即 `return None`——与降级耗尽同出口，reflection/planner 既有 None 降级路径已保证「取消/超时后不再发起新调用」（reflection `critique is None → 采用最近稿结束`；planner `plan is None → guard 复查拦截 ReAct 兜底`）。
3. `_call_generate` 的 `except Exception` 对**内置 `TimeoutError`** 特判直抛（保留执行终止语义）；`APITimeoutError` / `httpx.TimeoutException` 仍走可恢复路径（`decide_downstream_error`）不误伤网络重试。
4. reflection/planner 在 execute 现算 `deadline`，`_critique`/`_refine`/`_plan`/`_replan`/`_summarize` 加形参透传；`_replan_loop` 内现算 deadline 传 `_replan`。
5. reflection/planner 的 `except AppError` 改返回 `usage or None`（不丢已累加用量，调用方既有 `if xxx_usage: merge` 自然纳入成本计量）。

不做（明确划界）：正在进行的单笔 generate（限流排队 / SDK create 中）不优雅打断（对齐 ADR Decision 8「限流排队不提前打断，绝对截止硬取消总兜底」）；不给 reflection/planner execute 外包 `asyncio.timeout`；不加内部终止异常类型。

## 验证

- 失败先行 7 红转绿（`test_generate_structured.py`）：首级前 cancel / 降级链中途 cancel / 截断扩容前 cancel / deadline 已过 / deadline 未到对照 / 中断 usage 保留 / 内置 `TimeoutError` 直抛不降级。
- 既有 `test_generate_recoverable_exception_returns_none` 改向：原用内置 `TimeoutError` 冒充网络可恢复超时（恰是 todo §4 警告的混淆）→ 改用 `httpx.ReadTimeout`（classify RETRYABLE），内置 `TimeoutError` 保留为「整体期限终止」语义。
- 接线/贯穿新增：reflection 透传 cancel/deadline + 拒答上抛路径 usage 保留（`test_reflection.py` +2）；planner 透传 cancel/deadline（`test_planner.py` +1）。
- 全量 `uv run pytest` **867 passed**；`verify_alignment` 通过。

## 教训

- **执行护栏的粒度必须锚定「真实请求前」而非「策略阶段入口」**：结构化三级降级链是单次 Facade 调用内的多条真实请求，策略层护栏覆盖不到内部循环——与 REASON-001（预算锚定「LLM 调用前」而非「工具调用后」）同一教训在集成层重现。
- **内置 `TimeoutError` 是整体期限终止信号，不是网络可恢复超时**：`asyncio.timeout` 到期抛的就是它；凡经 `except Exception` 兜底并 `classify RETRYABLE` 的层都会把它当可恢复错误吞掉降级——termination 语义丢失。网络超时用 `APITimeoutError`/`httpx.TimeoutException` 表达，二者勿混。
