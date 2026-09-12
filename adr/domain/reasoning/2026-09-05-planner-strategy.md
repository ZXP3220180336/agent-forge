# Planner Strategy（Plan-then-Execute 单 Agent 编排）结构与契约决策

> 日期：2026-09-05 ｜ 层级：domain/reasoning + domain/agent

## Context

- 产品主链路第一步 = 主 Agent 拆分（良率异常 → 排查步骤）。需落地 Plan-then-Execute 单 Agent 编排作为 Orchestrator 拆分的语义底座：规划 → 逐「步」执行 → 证据链汇总。Phase C 子 Agent 用 `ReActStrategy` 排查，规划后每步执行应复用同一引擎。
- 领域层已定结构范式：`reasoning/` 原子推理策略（ReActStrategy / ReflectionStrategy 各自 + Outcome + 内部 schema），`agent/` 编排桥接（ReActAgent / ReflectionAgent 继承 BaseAgent）。原始领域层计划（domain-layer-plan-tmp）曾把 PlannerAgent 整体规划在 `agent/`（planning 视为编排职责），且设计执行阶段复用裸 `execute_tool_calls` 原语——与「每步跑 agent 循环 / 护栏全套」的实际价值冲突。
- 复用 `ReActStrategy.execute` 的另一考量：每步执行需要与 chat 前台一致的 SSE 事件流（工具/消息事件逐 token 可见）+ 每步受 `max_execution_time`（全局墙钟 → 剩余预算）、cost_limiter、错误分发等护栏约束；裸 `execute_tool_calls` 无这些护栏，需本模块自造一整套执行循环（回归 Reflection 曾走过的「重造护栏」弯路）。

## 工业级参照（调研详情）

> 调研范围：工业 Plan-and-Execute / LLMCompiler / MetaGPT / 结构化规划与工具最小权限实践，确立本方案采用路径。

- **Plan-and-Execute**（论文 + 主流框架实现）：规划器产出目标式步骤清单，执行器逐「步」跑 agent 循环；规划与执行显式分离，规划器不执行、执行器不规划。LangChain `PlanAndExecuteAgentExecutor` 即 executor 逐 plan step 调 agent loop——每步是完整 agent 循环而非单次工具调用。
- **LLMCompiler**：规划器产出带依赖的 DAG（plan + DAG，而非线性步骤），调度器按 `depends_on` 并行执行可并行任务。核心启示：**依赖建模走「DAG + 调度」是并行场景的解法**，单 Agent 串行场景 `depends_on` 退化为顺序纪律断言，不必引入调度器。
- **MetaGPT SOP**：多角色流水线（PM / 架构师 / 工程师），角色分工产出经标准操作流程接力——多 Agent 协作形态，非单 Agent 编排原语职责。
- **结构化规划优先**：计划经受约束的结构化产出（JSON Schema）而非自由文本解析；langchain 等框架对规划步骤提供格式校验，弃「让模型写文本步骤再正则拆」的脆弱路径。
- **工具最小权限（introspection 目录）**：规划器「知道有哪些工具、可指名使用」，但调用权只授执行器——OpenAI Agents SDK 等把 planner 与 executor 的 tool access 分开配置；给规划器真工具权会使其偏离计划职责并引入副作用风险。

## Decision

1. **两层结构 + 「生命周期依赖」分界标准**：策略实现落 `app/domain/reasoning/planner.py`（`PlannerStrategy` + `PlannerOutcome` + 四 schema 常量），桥接落 `app/domain/agent/planner.py`（`PlannerAgent`）。分界标准：模块是否依赖 Agent 生命周期（`AgentContext` / 状态机 / hooks / `run()` 编排与结果组装）——依赖 → `agent/`（桥接，收 AgentContext 透传）；纯算法收标量参数 + 构造注入端口 → `reasoning/`（策略，可独立测试）。三策略同构：ReActStrategy / ReflectionStrategy / PlannerStrategy 均在 reasoning/，ReActAgent / ReflectionAgent / PlannerAgent 均在 agent/。`reasoning/planner.py` 依赖方向 reasoning → ports + shared + prompts（不 import agent/），内部持 `ReActStrategy`（构造注入同款端口）。
2. **每步复用 `ReActStrategy.execute`（取代计划原文「execute_tool_calls」）**：执行阶段每步 = `ReActStrategy.execute(step.description, 步隔离消息, ...)` 受控小跑——护栏（错误分发 / 停滞检测 / cancel / 超时 / 上下文预算 / cost_limiter）+ SSE 事件流（工具事件透传）+ `ReActOutcome` 统一归并（usage/iterations/tool_calls）全套免费；裸 `execute_tool_calls` 仅保留为无护栏的工具并行原语，不再作为编排执行入口。`max_iterations` 语义 = 每步小跑迭代上限（步骤级），总预算由 `max_execution_time`（全局墙钟，每步转剩余预算 `_remaining_time`，下界 0.05s）+ cost_limiter 兜底。
3. **replan 取舍 + `max_refine_rounds` 语义复用**：仅**步骤失败**（react 非成功或无产出）触发重规划，成功路径不额外付费；`_replan_loop` 至多 `max_replan_rounds`（默认 2）次，产出新尾替换未执行部分、基于已完成步骤继续。重规划预算**复用 `AgentContext.max_refine_rounds`**（Reflection 修正 / Planner replan 共用「修复尝试上限」语义，字段语义在注释与文档声明），不新增字段。replan 新尾 id 续接 `max(executed.id)+1` 单调重编号防依赖错位。
4. **新增 `AgentErrorKind.PLAN_FAILED`（13→14 类，默认 CONTINUE）**：规划 / 重规划 / 汇总三处结构化调用失败统一走独立 kind 分发（不复用 `STRUCTURED_INVALID` 语义不符 / `REFUSED` 默认 STOP 硬停）。动作：CONTINUE=降级（规划失败→全量 ReAct 兜底 / 汇总失败→纯文本拼装，默认）/ STOP=硬失败 / RAISE=抛 `AgentRunError`。捕获范围 AppError 全家族，非 AppError 编程错误不吞冒泡；结构化截断集成层短路返回 None 走 None 降级，不进入分发。
5. **每步上下文隔离**：`_step_messages` 每步组装独立上下文 = system 前缀 + 已完成步骤摘要（`_executed_summary`，单条截断 500 / 总量 4000，只带结果不带原始工具 transcript）+ 当前步骤指令——不累积原始工具 transcript（防跨步 token 线性膨胀 + 步骤独立）。步骤自包含前提：需要上一步数值就在摘要里带，未覆盖则应合并成一步。
6. **规划器工具处理定稿**：`_plan` / `_replan` 经 `generate_structured` 恒不传 `tools` → **无真 tool_calls**（规划失败零工具副作用）；工具认知走 introspection 目录文本（`_tool_catalog` 名 + 描述注入规划 prompt，供指名）；步骤 `description` 写工具动作 = **正常计划语义（非噪音）**（执行器的每步 ReAct 才是工具调用发生的唯一位置）。

## Consequences

- ✅ 向后兼容：`ReActStrategy.execute_tool_calls` 保留（ReActAgent 转发、test_agent 引用零改动）；新增 `AgentErrorKind.PLAN_FAILED` 只扩枚举 + 默认动作表，不改变既有 kind 行为。
- ✅ 产品导向：PlannerAgent = 主 Agent 拆分原语，`plan` / `steps_executed` / `replan_rounds` / `degraded` 进 `AgentResult.metadata` 供 Phase C Orchestrator 与证据链报告消费；每步执行与 Phase C 子 Agent 同一引擎（ReActStrategy）。
- ✅ 新测试：`tests/unit/test_planner.py` 13 用例（三阶段全路径 / usage 累计与 done 口径 / plan None→ReAct 兜底 / AppError→PLAN_FAILED 分发 CONTINUE·RAISE·STOP / 步骤失败→replan / replan 耗尽→纯失败 / 汇总 None→纯文本 / schema 严格性 / cancel·成本护栏 / replan id 单调重编号）；`tests/unit/test_planner_agent.py` 3 用例（端到端 metadata 映射 / outcome None 兜底 / ctx.max_refine_rounds→replan 预算）；`tests/unit/test_prompts.py` 5 用例（planning 三 builder + reflection/system 冒烟）。
- ⚠️ **语义取舍（知情）**：
  - **串行单 Agent**：依赖并行排查价值在 Phase C Orchestrator 释放；单 Agent 层 `depends_on` 是顺序纪律断言，不承担 DAG 调度（见 benchmark 取舍表）。
  - **replan 仅失败触发**：成功路径不付出重规划检查的额外调用成本，但失败恢复能力上限 = max_replan_rounds（默认 2）——耗尽后走部分汇总 / 纯失败降级。
  - **规划失败也付费一次**：规划是结构化付费调用，失败即进入兜底；降级不抛错（best-effort），error 保留原 AppError 文本供诊断。
- 📌 关联：本决策修正领域层计划原文「PlannerAgent 整体归 agent/ + 执行复用 execute_tool_calls」的实现路径（落地为两层结构 + 每步复用 ReActStrategy.execute），见 [domain-layer-plan-tmp](../../../docs/history/domain-layer-plan.md)（该文为计划原文，以代码为准）。
