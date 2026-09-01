# Reflection 推理策略设计（三阶段分离 + Grounding 反内在自查）

> 日期：2026-08-31 ｜ 层级：domain/reasoning + domain/agent

## Context

- Reflection 是领域层推理策略剩余缺口（`reasoning/reflection.py` / `agent/reflection.py` 原空壳），产品价值 = 证据链报告的语义自查（防伪证据）。
- 工业级红线（Huang et al. ICLR 2024 反证《LLM Cannot Self-Correct Reasoning Yet》）：**纯内在自查（intrinsic）不可靠，甚至让模型把对的改错**；critic 必须被外部信号（工具结果 / 证据链）锚定（CRITIC / Reflexion 正证）。
- 仓库已有底座：`ReActStrategy.execute(output_schema=...)`（工具收集 + final_answer 结构化，证据链在 outcome.tool_calls）、`ErrorHandlerRegistry`（共享内核错误分发）、`ContextBudgetPort` / `CostLimiterPort` 护栏。

## Decision

1. **三阶段显式分离**：生成（收集+初稿）→ 自查（Critic）→ 修正（Refine）独立方法，禁止混在单一 prompt——生成与评判是不同认知任务。
2. **Grounding 为核心**：`CRITIQUE_PROMPT` 注入证据链记录 + 初稿，自查清单穷举 9 维度（grounding 为第 1 维），杜绝内在自查。自查范围 = 提示词清单（Scope 盲区教训，清单为唯一自查范围）。
3. **生成阶段复用 ReAct**：`ReActStrategy.execute(output_schema=REFLECTION_SCHEMA)`——工具收集 + final_answer 结构化初稿一次完成（Reflexion「生成即工具增强」），护栏（cost/context/cancel）透传内部 `_react`。
4. **新增 `AgentErrorKind.CRITIQUE_FAILED`（默认 CONTINUE）**：自查/修正失败走独立 kind——`STRUCTURED_INVALID` 语义不符（回喂自纠 vs 降级 best），`REFUSED` 默认 STOP 会硬停整个 Agent（违背「返回 best 而非抛错」）。动作：CONTINUE=降级采用最近稿（默认）/ STOP=整个失败 / RAISE=抛 `AgentRunError`。
5. **降级路由（best-effort）**：react 失败 / 无 structured / 自查失败 / 修正失败 → 采用最近稿（degraded=True），不抛错。
6. **Schema 模块常量 + 构造注入覆盖，不进 AgentContext**：schema 是结构性领域契约非每次运行的标量参数；`AgentContext` 只加 `max_refine_rounds`（生产值经 `agent_max_refine_rounds` 注入）。
7. **真迭代（修正后重新自查）**：修正 refined 后**重新自查 refined**，ok 则采用（early exit）、新 issues 再修正，直到通过或达 `max_refine_rounds` 上限（cap）——工业标准（Self-Refine 每轮新 feedback；LangGraph「revise 后必 re-reflect，否则循环无效」）。复用同一批 issues 反复修正 = 反模式，见 [REASON-009](../../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md)。

## Consequences

- ✅ 核心必备 12 项工业基准全部落地（1 项 ⚠️ cost 缺口），见 reflection_benchmark.md。
- ✅ Grounding 直接踩产品证据链亮点（结论可回溯 / 无编造 / 置信度分级 / explicit_abstention 显式放弃）。
- ✅ **真迭代已实现**：修正后重新自查（early exit + cap），修正初版「refine 后不二次自查」决策（见 [REASON-009](../../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md)）。
- ⚠️ **cost 缺口**：`generate_structured` 契约不返回 usage → critique/refine 实际 token/成本不可精确计量。取舍：收集阶段（token 主体）经内部 react 的 cost_limiter 完整护栏；critique/refine 由 `max_refine_rounds`（≤2 次 fast 调用）结构性兜底。升级路径：扩展 `generate_structured` 回传 usage（跨层改造，后续评估）。
- ⚠️ **共享内核影响**：`AgentErrorKind` 12→13 类（error_handling.py），所有 Agent 共享——Reflection 独有 kind，ReAct 不受影响。
- 📌 增强项（跨轮次记忆 / 多 critic / HITL / 回流微调）按「不做或预留」降级（产品导向，见 reflection_benchmark 产品视角）。
