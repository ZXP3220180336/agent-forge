# 成本上限自动停机（CostLimiterPort 注入 + COST_EXCEEDED 分发）

> 日期：2026-08-30 ｜ 层级：domain + application

## Context

- 对标增强项 #20 ⚠️：`CostTracker` 记账能力已存在（`app/integration/llm/cost_tracker.py`，静态 USD 计算，硬编码 `MODEL_PRICING`），但**未接入运行时**、**无 cost ceiling 自动停机**——ReAct 循环每轮累计 token 用量却不折算成本，长任务可无限花费。
- 工业界机制调研：Claude Agent SDK cost ceiling（预算触发自动停机）、OpenAI max_completion_tokens / 成本护栏——**超预算停机**是长任务经济的标准护栏。
- 分层调研（DDD）：成本判定是**横切能力**（所有 Agent 模式共享），且 react.py 依赖方向「只依赖 ports + shared + 标准库」——**不可 import 集成层 CostTracker**；策略层又无 model 名（`model_key` 在 `LLMGateway.async_generate` 内部）。→ 成本判定经**端口注入**（镜像 ContextBudgetPort 先例），model 与 ceiling 封装在应用层实现。

## Decision

1. **端口注入**：新 `domain/ports/cost_limiter.py`（`CostLimiterPort.check(usage) -> (exceeded, cost_usd)`，无状态纯函数——容器可安全共享单例，并发请求不串扰）；应用层 `CostLimiter` 结构实现。`ReActStrategy` / `ReActAgent` 构造注入（与 context_budget / error_handlers 同构，端口依赖不进 execute 标量 / AgentContext）。
2. **成本估算归属 LLMGateway**：成本估算是 LLM 能力（模型定价），并入 `LLMGateway` 端口（`calculate_cost(usage, model) -> dict`，由 `LLMService.calculate_cost` Facade 结构实现），不设独立成本端口——`CostLimiter`（应用层）经 `LLMGateway` 端口接入，应用层**不直接 import 集成层、不触及 LLM 子组件（CostTracker）**（对齐 ContextManager 经 `LLMGateway.count_*` 端口先例；端口数量 7→6→5 更内聚）。
3. **口径美元**：`CostTracker.calculate(usage, model)["cost_usd"]` 与 ceiling 比较（严格 `>` 超限）。model 取 `settings.llm_model_id`（ReAct 主推理路径）；空/未知 model 走 `DEFAULT_PRICE` 兜底（对表内模型高估 → 停机偏早，方向保守安全）。
4. **错误分发停机**：新 `AgentErrorKind.COST_EXCEEDED`（终结性，默认 STOP）。主循环**每轮 usage 累加后** `check(累计 usage)`，超限走 `_finalize_cost_exceeded`（对齐 `_finalize_timeout` 降级：用 last_result 组装 outcome、error 记录「成本超限（累计 $X）」、done 恰一次）。检查点置于 error 判断前：预算超限时不允许失败重试 / 工具执行再产生付费调用或副作用。

   **检查点位置论证（为何「调用后」而非「调用前」；2026-09-06 工业调研补证）**：
   - **信息时序决定**：单次调用的实际成本只能在返回（usage 落地）后得知——「累计实际成本超限」的判定在调用返回前不可能精确做出，故**每笔付费调用之后立即检查是必要且唯一精确点**。工业实证：Anthropic Claude Agent SDK `max_budget_usd` 即「每笔 API 调用完成后检查」，官方明文接受最终成本可略超预算（≤1 次调用）；OpenAI / LangGraph / SMOLagents 循环内均无成本上限（OpenAI 平台层硬上限为异步事后追踪，明文承认传播延迟超支）；Langfuse / LangSmith 成本为事后记账、不拦停。无任何主流 Agent 循环在调用前用纯累计实际成本拦截——成本未知，做不到。
   - **纯前移 = 逻辑死代码**：本循环单顺序串行（每轮恰一笔付费调用，所有 continue 路径均回循环顶），超限轮已在调用后停机、执行流走不到下一轮顶部——复用同一累计函数的「循环顶 pre-call」永不触发。工业反证：LiteLLM 遗留 pre-call hook（只读累计实际花费）在网关联单下因每笔并发请求都是新顶部而不死，但挡不住并发同时越界，已被「估算 + 预留 + 对账」取代——其局限恰印证该模式在单串行循环内即死代码。
   - **调用后立即检查的额外价值**：超限轮**不放行本轮工具执行**（掐断越界轮副作用）——若检查点移到循环顶，越界轮会先完成工具执行才在下一轮顶部被察觉，副作用不受控。

   出处：Anthropic Claude Agent SDK cost-tracking 文档（max_budget_usd 语义）；OpenAI Spend limits 文档（平台上限异步事后追踪）；LiteLLM PR #27021 / #27539（估算预留层演进）；Portkey / llm0（转发前估算拦截，见升级路径）。
5. **成本不记录领域 outcome**：成本可由 `usage` + `LLMGateway.calculate_cost` 确定性推导；领域层记录成本会把定价口径带进领域（职责越界）。停机可见性由 `outcome.error` 文案覆盖。
6. **配置**：`agent_max_cost`（float | None，USD；None=不启用，validator 拒负值）。装配根 `container.cost_limiter` 单例组装；`cost_limiter=None` 时 ReAct 循环整段检查零开销。

## Consequences

- ✅ 增强项 #20 闭合：成本超限自动停机（默认 STOP 降级），与 TIMEOUT / MAX_TURNS 同属终结性护栏，按 kind 可覆盖（handler 可 RAISE 上抛 / CONTINUE 被忽略）。
- ✅ 依赖方向保持：react.py 仍只依赖 ports + shared；定价（集成层）与上限（配置）都封装在应用层实现。
- ✅ 嵌套复用一致（跨策略落地）：planner 每步 react 子跑经 `ReActStrategy.execute(baseline_usage)` 透传 planner 累计——子跑内每轮即按**累计**成本检查（对齐本 ADR「调用后立即检查」），子跑中途累计越界在越界轮停（掐断该轮工具），报告口径仍为子跑局部（各归并一次防双计）；reflection 收集 react 为单次前置、无需 baseline。
- ⚠️ 成本基于硬编码 `MODEL_PRICING`（ADR pricing-prefix-match-fixed-table 既有决策）：模型价格变动需代码更新，测试回归防护；默认价兜底对未知模型高估（停机偏早，方向安全）。
- ⚠️ 停机发生在「超限那一轮的 LLM 调用之后」——cost ceiling 固有：多付最多一轮（实际成本仅在调用返回后可知，调用前无法精确判定；见 Decision 4 论证）。超限轮工具不放行（副作用一并掐断）。
- 📌 升级路径：**估算式预检（pre-call 软闸）**——当出现「绝不允许某单轮过贵」（剩余预算小且下一轮上下文 / max_tokens 巨大）或**真并发**（并行子 Agent 各自发请求，在途互相看不见，单串行「每轮查一次」不再最紧）时，需在 post-call 主闸之上叠第二道软闸：发出前估算本笔成本（输入上下文 token + 输出按 `max_tokens` 上界）超预算即拒绝，调用后按实际 usage 对账。需扩展 `CostLimiterPort`（现 `check` 为纯累计、无估算入参）并权衡上界估算的误杀代价（工业实证：Portkey 转发前拦截、llm0 按 max_tokens 上界估、LiteLLM reservation 层）。
- 📌 与上下文预算互补：trim 在 LLM 调用前（减少发送 token），cost check 在调用后（审计花费）；与 #19 断点续跑互为「长任务经济性」两面——成本停机后若无 checkpoint，恢复 = 重跑（再次付费）。
