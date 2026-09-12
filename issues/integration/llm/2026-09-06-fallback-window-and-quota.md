# LLM-041 fallback 备用链路沿用主窗口且绕过限流闭环

> 发现：2026-09-06 ｜ 状态：✅ 已修复 ｜ 模块：llm_service / request_budget / retry / streaming_rectifier
> 关联：请求准入 [ADR](../../../adr/integration/llm/2026-09-06-request-context-budget.md)（Decision 3/7/9）· fallback 同 provider [LLM-012](2026-08-16-fallback-same-provider.md)

## 发现

会话交接核验指出：`_build_fallback_fn` 预算校验沿用**主调用 model_key 窗口**（`RequestBudgetManager.get(model_key)`），且 fallback 请求**直接 SDK create**、绕过 reserve/settle 限流闭环；配套测试 `test_open_circuit_fallback_shares_main_window_guard` 把「共享主窗口」固化为期望行为。

## 分析

1. **同 provider ≠ 同窗口**：LLM-012 只约束 base_url/密钥复用（同端点），上下文窗口是**模型实体属性**——备用模型窗口小于主模型时，按主窗放行会让「最需要兜底」的备用链路在 provider 侧报窗口错误而兜底失败。
2. **配额保护缺失**：fallback 不参与限流则无排队/退避、无配额记账；取消时也无 Reservation 可退款。
3. **测试前提错误且隐蔽**：手动置熔断 `OPEN` 但 `_last_failure_time` 已过 `recovery_timeout` 时，`allow_request` 自动转 `HALF_OPEN` 放行一个**主链路探针**——原「共享主窗口」用例实际测的是 HALF_OPEN 探针里主链路预算闸，**从未真正走到 fallback**（断言 `model_key == "fast"` 恰好掩盖）。

## 修复

- **独立 `fallback` 键配置**：settings 新增 `llm_fallback_context_window_tokens` / `llm_fallback_rpm` / `llm_fallback_tpm`；container 把 `fallback` 键注册进 `RequestBudgetManager` 与 `ReservationLimiterManager`。配额独立池为当前体系默认（`main`/`reasoning`/`fast` 本就按模型键独立桶）；若供应商对主/副模型共享配额则合并记账——作为 ADR 升级路径，不硬编码共享。
- **fallback 进统一请求入口**：`_build_fallback_fn` 改为 async 闭包，经 `_budget_guarded_call`（fallback 键窗口 + fallback 独立配额池 reserve → create），Reservation 写入与主请求**共享的 `active`**，结算/退款复用主链路同一收尾路径；`active` 构造前移（`async_generate` / `generate`）。
- **预算拒绝直抛不包装**：`retry.execute` 两处 fallback 失败分支（CLOSED 重试耗尽 / HALF_OPEN 探针失败）对 `ContextWindowExceededError` 直接 re-raise——否则被 `raise last_exc from fallback_exc` 包成主网络故障 cause，下游 `decide_downstream_error` 当可恢复错误降级，甚至触发结构化/整流再调主（付费但必然再超限）。
- **取消竞态**：`_budget_guarded_call` 在 reserve 返回后、create 前复查业务 `cancel_event`，命中则 `cancel()` 退款并抛整流器业务取消信号 `_StreamCancel`，由 `rectified_stream` 映射为用户取消出口（不发起请求）。

## 验证

- 新增/改造用例：fallback 独立键窗口拒绝（`model_key == "fallback"`）、主窗可容/备用窗拒绝正反例、CLOSED / HALF_OPEN 主失败后 fallback 超限直抛（不包主错误）、fallback 成功走独立池 reserve + settle 恰一次、reserve 排队期间业务取消 → 退款且 SDK 零调用。
- `_open_circuit` 测试辅助把 `_last_failure_time` 置向未来，确保真走 fallback（而非 HALF_OPEN 主链路探针）。
- 全量 `uv run pytest`：855 passed；`verify_alignment` 通过。

## 教训

1. 熔断状态机测试「置 `_state = OPEN`」不等于「触发 OPEN 拒绝」：冷却期可能已过而自动转 `HALF_OPEN` 并放行主链路探针。断言目标分支前要先让被测条件在状态机上成立（这里把 `_last_failure_time` 置向未来）。
2. 「测试通过 ≠ 行为正确」：绿灯若固化了错误前提（共享主窗口），反而成为重构障碍——按目标语义重写断言比迁就绿灯更重要。
3. fallback 是兜底路径，但它仍是**真实付费调用**：窗口、配额、取消、结算四项治理应与主请求同权，否则在最需要兜底时失效。
