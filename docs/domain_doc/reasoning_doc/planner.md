# PlannerStrategy 设计文档

> **模块**：`app/domain/reasoning/planner.py`
> **更新日期**：2026-09-10
> **职责**：Planner 原子推理策略——Plan-then-Execute 单 Agent 编排（规划 → 执行 → 汇总）
> **状态**：✅ 已实现
> **配套**：桥接见 [agent/planner.py](../agent_doc/agent.md)；工业级对标见 [planner_benchmark.md](planner_benchmark.md)

---

## 📋 目录

- [PlannerStrategy 设计文档](#plannerstrategy-设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [三阶段显式分离](#三阶段显式分离)
    - [每步隔离上下文](#每步隔离上下文)
    - [depends\_on 顺序纪律断言（串行单 Agent）](#depends_on-顺序纪律断言串行单-agent)
    - [introspection 工具目录（规划器最小权限）](#introspection-工具目录规划器最小权限)
    - [Replan 触发与 next\_id 单调重编号](#replan-触发与-next_id-单调重编号)
    - [PLAN\_FAILED 错误分发](#plan_failed-错误分发)
    - [降级路由（best-effort）](#降级路由best-effort)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [PlannerStrategy](#plannerstrategy)
    - [PlannerOutcome（结果载体）](#planneroutcome结果载体)
    - [Schema 契约](#schema-契约)
  - [执行流程](#执行流程)
  - [对外接口](#对外接口)
  - [边界情况](#边界情况)
  - [配置项清单](#配置项清单)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [问题记录](#问题记录)
  - [相关文档](#相关文档)

---

## 设计目标

1. **Plan-then-Execute 三阶段显式分离**：规划（结构化产出依赖步骤）→ 执行（逐「步」小跑）→ 汇总（结构化证据链报告）独立方法——规划与执行是不同认知任务，规划器只产计划不执行（工业 Plan-and-Execute）
2. **每步复用 `ReActStrategy.execute`**：执行器逐「步」= 被当前步骤约束的 ReAct 小循环，而非重造工具循环——错误分发 / 停滞检测 / cancel / 超时 / 上下文预算 / cost_limiter 全套护栏免费，`ReActOutcome` 产出统一归并。cost 护栏跨阶段贯通：`_run_react` 透传 planner 累计（已完成步骤 + 结构化用量）作 `execute(baseline_usage)`——子跑内每轮即按**累计**成本检查（子跑中途累计越界即在越界轮停），报告口径仍为子跑局部（各归并一次防双计）
3. **结构化规划优先**：计划经 JSON Schema（`PLAN_SCHEMA` / `REPLAN_SCHEMA`）产出，弃文本解析——步骤形状 / depends_on / 上限程序化强制
4. **串行单 Agent**：`depends_on` 是顺序纪律断言（列表序即合法拓扑序），执行按列表序出队；并行 DAG 调度留 Phase C Orchestrator
5. **Replan 仅步骤失败触发**：不做每步 replan；重规划预算复用 `AgentContext.max_refine_rounds`（Reflection 修正 / Planner replan 共用「修复尝试上限」语义）
6. **失败降级（best-effort，不抛错）**：规划失败 → 全量 ReAct 兜底；步骤失败 replan 耗尽 → 有成功步则部分汇总报告、否则纯失败；汇总 None → 纯文本拼装

## 核心概念解释

### 三阶段显式分离

| 阶段 | 职责 | 调用 |
| --- | --- | --- |
| A 规划 | 产出 goal + 依赖步骤（`PLAN_SCHEMA`） | `generate_structured`（默认 fast 模型） |
| B 执行 | 串行逐「步」小跑（每步复用 ReAct）+ 步骤失败触发 replan | 每步 `ReActStrategy.execute(step.description, 步隔离消息)`；`generate_structured(REPLAN_SCHEMA)` 产新尾 |
| C 汇总 | 基于各步结果产出证据链报告（`RESULT_SCHEMA`） | `generate_structured`（默认 fast 模型） |

### 每步隔离上下文

`_step_messages` 为每步组装独立上下文：system 前缀 + 已完成步骤摘要（每步只带结果摘要 [:500]，不带原始工具 transcript，整体 [:4000] 限长）+ 当前步骤指令——**不累积原始工具 transcript**，防跨步 token 线性膨胀、保步骤独立。前提：步骤自包含（需要上一步数值就在摘要里带，未覆盖则应合并成一步）。

### depends_on 顺序纪律断言（串行单 Agent）

`PLAN_STEP_SCHEMA.depends_on` 必填（防模型跳过排序思考）；`_normalize_steps` 程序化赋值单调 id（初始 1 起），丢弃自引 / 前瞻 / 未知引用——normalize 后依赖必然 ⊆ `completed` ∪ 本列表更前位置。单 Agent 串行下列表序即合法拓扑序，`depends_on` 是纪律断言而非调度依据；并行 DAG 调度留 Phase C Orchestrator。

### introspection 工具目录（规划器最小权限）

规划器**知道**有哪些工具（`_tool_catalog` 名 + 描述文本注入规划 prompt，可指名使用），但**不调用**：`_plan` / `_replan` 经 `generate_structured` 结构化调用，恒不传 `tools` → 无真 `tool_calls`。步骤 `description` 写工具动作是**正常计划语义（非噪音）**——工具调用权只在每步执行器（ReAct）。最小权限：规划失败也不产生任何工具副作用。

### Replan 触发与 next_id 单调重编号

仅**步骤失败**（react 非成功或无产出）触发 replan（`_replan_loop`，至多 `max_replan_rounds` 次）。replan 基于已完成步骤 + 失败步骤产出**新尾**（`REPLAN_SCHEMA.steps`）替换未执行部分；新尾 id 续接已执行步最大 id 之后（`start_no = max(executed.id) + 1`）单调重编号。子 ReAct 返回后先吸收其成果、usage 与迭代，再经统一 guard 决定是否终止，避免取消时丢失已产生的成本和部分步骤记录。

### PLAN_FAILED 错误分发

规划 / 重规划 / 汇总三处结构化调用失败（`generate_structured` 抛 AppError）统一走 `AgentErrorKind.PLAN_FAILED`（默认 CONTINUE）——独立 kind 而非复用 `STRUCTURED_INVALID`（语义不符）。动作：CONTINUE=降级（规划失败→全量 ReAct 兜底；汇总失败→纯文本；默认）/ STOP=硬失败标记 / RAISE=抛 `AgentRunError`。捕获范围 **AppError 全家族**（拒答 / 工具调用 / 熔断等不可恢复错误）；非 AppError 编程错误不吞、向上冒泡（fail fast）。结构化截断由集成层短路返回 None → 走 None 降级路径，不进入本分发。

### 降级路由（best-effort）

| 情形 | 处理 |
| --- | --- |
| 规划返回 None（结构化降级耗尽） | 全量 ReAct 兜底，degraded=True |
| 规划抛 AppError（PLAN_FAILED 默认 CONTINUE） | 全量 ReAct 兜底，degraded=True |
| PLAN_FAILED handler STOP | 硬失败（success=False），不做有效兜底 |
| 计划 normalize 后无步骤 | 降级失败 |
| 步骤失败 → replan 新尾 | 替换未执行部分继续（degraded=False，若最终恢复） |
| replan 耗尽 / 失败 + 有成功步 | 尝试结构化汇总产部分报告；None → 纯文本拼装（degraded=True） |
| replan 耗尽 / 失败 + 无成功步 | 纯失败 partial（degraded=True） |
| 汇总返回 None / AppError | 纯文本拼装（degraded=True） |
| 汇总成功 | 采用结构化（degraded=False） |

## 架构总览

```text
PlannerAgent._strategy_cycle()（agent/ 层编排：生命周期 / 状态 / 结果组装）
        ▼ execute()
PlannerStrategy.execute()（三阶段）
    ├── 阶段 A：generate_structured(PLANNING_PROMPT + 工具目录, PLAN_SCHEMA)
    ├── 阶段 B：while pending: 串行每步
    │       └── ReActStrategy.execute(step.description, 步隔离消息)   ← 每步被计划约束的 agent 循环
    │               └─ 步骤失败 → _replan_loop：generate_structured(REPLAN_PROMPT, REPLAN_SCHEMA) → 新尾继续
    └── 阶段 C：generate_structured(SUMMARIZE_PROMPT, RESULT_SCHEMA) → 证据链报告
        └── 错误分发：ErrorHandlerRegistry（PLAN_FAILED，default CONTINUE）
```

**依赖方向**：`reasoning → ports + shared + prompts`（不 import agent/）；内部持 `ReActStrategy`（构造注入同款端口），被 agent/planner.py（PlannerAgent）编排调用。

## 组件详解

### PlannerStrategy

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(llm, tools, context_budget=None, error_handlers=None, cost_limiter=None, plan_schema=None, replan_schema=None, result_schema=None, plan_model_key="fast", summarize_model_key="fast")` | 构造 `_react = ReActStrategy(...)`（护栏透传）；schema / 结构化模型键可注入覆盖 |
| `execute` | `(user_input, messages, *, max_iterations, temperature, max_tokens, max_execution_time=None, max_context_rounds=None, max_context_tokens=None, max_empty_retries=2, max_llm_fail_retries=2, max_tool_protocol_retries=2, max_same_action_turns=3, max_replan_rounds=2, tool_timeout=None, tool_max_retries=None, stream_mode=True, cancel_event=None) -> AsyncGenerator[str]` | 三阶段主流程（见「执行流程」）；yield SSE 事件，结果写入 `outcome`；协议修正上限透传每步 ReAct |

私有辅助：`_absorb_react`（react 子跑 usage/iterations 归并）· `_tool_catalog` / `_step_messages`（工具目录 / 每步隔离上下文，已完成步骤摘要已并入）· `_finalize_partial` / `_plain_summary`（纯文本降级）· `_finalize_guarded_summary`（汇总成功后护栏命中时保留已生成结构化结果）· `_normalize_steps`（id 单调赋值 + 依赖清洗）· `_plan` / `_replan` / `_summarize`（结构化调用；每笔调用前与 usage 归账后经共享 `_common.evaluate_guard` 复查；每步 / 兜底 react 子跑经 `_run_react` 透传 planner 累计作 `baseline_usage`；PLAN_FAILED 分发经 `_common.dispatch_error`）· `_finalize`（收尾 outcome + done 事件）。

### PlannerOutcome（结果载体）

| 字段 | 说明 |
| --- | --- |
| `content` / `reasoning` | 自由文本（ReAct 兜底 / 纯文本汇总路径 / summary 摘要） |
| `structured` | 最终 `RESULT_SCHEMA` 结构化（证据链报告） |
| `plan` | 生效计划 `{goal, steps:[{id, description}]}`；兜底路径 None |
| `steps_executed` | 步骤审计记录（含失败步：id / description / success / summary / error / content / tool_calls / iterations / total_tokens） |
| `replan_rounds` / `degraded` | 实际重规划次数 / 是否经降级路径 |
| `tool_calls` | 聚合证据链（各步 tool_calls 平铺） |
| `iterations` / `total_tokens` / `usage` | 统计（react 各步 + plan/replan/summarize 结构化全阶段累计） |
| `error` / `success` | 失败原因 / 是否成功 |

### Schema 契约

模块常量 + 构造注入覆盖（`plan_schema` / `replan_schema` / `result_schema`），不进 `AgentContext`：

- `PLAN_SCHEMA`：`goal`（重述目标 + 可判定成功标准）+ `steps[]`（minItems 1 / maxItems 6，每项 `PLAN_STEP_SCHEMA`）；required=[goal, steps]；列表顺序 = 推荐执行顺序
- `PLAN_STEP_SCHEMA`：`description`（目标式步骤：目的 + 建议工具与查询目标 + 产出形态 + 可判定完成标准；单次工具执行循环可独立完成）+ `depends_on`（前置步骤编号数组，必填防跳过排序思考）；required=[description, depends_on]
- `REPLAN_SCHEMA`：`reason`（重规划缘由，引用失败步骤）+ `steps[]`（替换未执行剩余部分的新列表，含失败步骤修订）；required=[reason, steps]
- `RESULT_SCHEMA`：`summary` + `conclusions[]`（claim + supporting_evidence[] + confidence 0~1）+ `next_steps[]` + `assumptions[]` + `explicit_abstention[]`；required=[summary, conclusions, next_steps, explicit_abstention]（产品「证据不足显式放弃」程序化强制）——与 `REFLECTION_SCHEMA` 同构但独立发布（结构化最终输出走产品主链路「证据链报告」方向；不 import reflection 避免模块耦合，允许未来分叉）

四个 schema 均 `additionalProperties: False` 强制形状（jsonschema 校验，test_schema_strict 覆盖）。

## 执行流程

```text
execute 入口：重置全部累计态（_structured_usage / _react_total_usage / _replan_used ...）
   │  工具目录 = _tool_catalog()
   ▼
阶段 A 规划：
   ├─ 终止/成本护栏（取消 / max_execution_time / cost_limiter → 未开始即停机降级）
   ├─ _plan：generate_structured(PLANNING_PROMPT+目录, PLAN_SCHEMA)
   ├─ plan=None →（PLAN_FAILED 已分发）→ 降级全量 ReAct.execute(剩余预算) → 收尾 degraded
   └─ normalize steps（id 1 起单调；空 → 降级失败）
   ▼
阶段 B 执行（while pending，每次顶部重查终止/成本护栏）：
   ├─ step = pending.pop(0)；_step_messages 组装隔离上下文
   ├─ ReActStrategy.execute(step.description, ...)  →  absorb usage/iterations
   ├─ cancel 置位 → 立即部分汇总终止（防误 replan）
   ├─ 判成功 = react success 且产出非空；记录 steps_executed
   ├─ 成功 → completed.add；继续下一 step
   └─ 失败 → _replan_loop（≤ max_replan_rounds）：
        ├─ generate_structured(REPLAN_PROMPT, REPLAN_SCHEMA) → 新尾 normalize（id 续 max+1）
        ├─ 有可执行新尾 → 回到 while 顶部继续
        └─ 耗尽/失败 → 有成功步则尝试部分汇总报告、否则纯失败 partial（degraded）
   ▼
阶段 C 汇总：
   ├─ 终止/成本护栏 → 部分进度降级
   ├─ _summarize：generate_structured(SUMMARIZE_PROMPT, RESULT_SCHEMA)
   ├─ result=None → 纯文本拼装降级（degraded）
   └─ 成功 → _finalize：PlannerOutcome + info + done（total_tokens = react 各步 + 结构化累计）
```

> done 抑制与口径一致（REASON-011 模式）：每步 ReAct 中间 done 在子跑事件透传时被抑制（execute 内 `_run_react`），事件流仅保留收尾 1 个 done；done 的 total_tokens 与 outcome 对齐（react 各步 + plan/replan/summarize 结构化累计）。

## 对外接口

> 对外接口 = 被 agent/ 层编排依赖的策略类。`PlannerAgent._strategy_cycle()` 委托 `PlannerStrategy.execute()`，并把 `PlannerOutcome` 映射为 `AgentResult`（plan / steps_executed / replan_rounds / degraded 进 metadata，供 Phase C Orchestrator 与证据链报告消费）。

| 策略类 | 契约（方法） | 说明 |
| --- | --- | --- |
| `PlannerStrategy` | `execute(...)` + `outcome` | 完整契约见上文；被 PlannerAgent 编排 |

## 边界情况

| 场景 | 处理 |
| --- | --- |
| 规划返回 None（结构化降级耗尽） | PLAN_FAILED 分发（默认 CONTINUE）→ 全量 ReAct 兜底（degraded=True） |
| 规划抛 AppError（拒答 / 工具调用 / 熔断） | PLAN_FAILED 分发 → 同上；handler STOP → 硬失败；RAISE → 抛 `AgentRunError` |
| 计划 normalize 后无步骤 | 降级失败（不发起任何执行付费） |
| 步骤失败 | 触发 replan（≤ max_replan_rounds）产新尾替换未执行部分；失败步留审计记录 |
| replan 耗尽 + 有成功步 | 尝试结构化部分汇总报告；None → 纯文本拼装（degraded=True） |
| replan 耗尽 + 无成功步 | 纯失败 partial（degraded=True） |
| 汇总返回 None / AppError | 纯文本拼装（degraded=True；STOP → 硬失败） |
| 结构化输出截断 | 集成层短路返回 None → 走 None 降级路径（本层无感知） |
| 步骤执行抛非 AppError 编程错误 | 不吞，向上冒泡（fail fast） |
| cancel 在步骤 react 中置位 | 子跑返回后先吸收 outcome/usage/iterations 并记录当前步骤，再按共享优先级终止并部分汇总；不进入 replan |
| 总时长超限（全局墙钟） | 每步 ReAct 转剩余预算（全局墙钟差额，下界 0.05s）；阶段顶部超限 → 采用已完成步骤 |
| 成本超限（cost_limiter） | 每步 react 子跑带 planner 累计 `baseline_usage`；plan/replan/summarize 调用前和归账后经 `evaluate_guard` 复查；超限后不空转 fallback/replan/summarize |
| 结构化降级链内取消/超时（E） | plan/replan/summarize 透传 `cancel_event` + 绝对 `deadline`；信号约束每笔 reserve/create/retry，并在 extract 最外层收敛 None；`plan is None` 后 guard 复查拦截 ReAct 兜底 |
| 规划后护栏命中（plan 已产出但未开工） | 仍给出契约形状 plan 快照（`_plan_payload` + normalize 赋 id、不含 `depends_on`），`steps_executed=[]`；文案区分「规划后中止」与「规划失败后中止」 |
| 子跑达迭代上限但有产出 | 判步骤成功（二维判据：react success 且产出非空），`sub.error` 只记录停机原因；不进入 replan |
| 每步隔离前提被破坏（需上一步数值未带进摘要） | 步骤自包含约束要求；未覆盖应合并成一步（提示层纪律） |
| 同一实例并发 / 多次 execute | 不并发复用——outcome 与累计态在每次 execute 覆盖，每次运行新建或串行读取 |
| 步骤产出为空 / react 子跑无结果 | 判步骤失败（无产出工件视为失败），走 replan |

## 配置项清单

无新增 settings 项。`AgentContext` 已有字段复用：

| 配置 | 类型 / 默认 | 说明 |
| --- | --- | --- |
| `agent_max_refine_rounds` | int / 2 | replan 预算经 `AgentContext.max_refine_rounds` 注入 `execute(max_replan_rounds)`（Reflection 修正 / Planner replan 共用语义） |
| `agent_max_iterations` 等 ReAct 护栏字段 | 复用 | `max_iterations` = **每步** ReAct 小跑迭代上限（步骤级）；temperature / max_tokens / max_execution_time（全局，每步转剩余）/ max_context_rounds·tokens / max_empty_retries / max_llm_fail_retries / max_tool_protocol_retries / max_same_action_turns / stream_mode 全部透传每步 ReAct |
| `llm_structured_max_tokens` | 默认预算 | 结构化调用（plan / replan / summarize）走 generate_structured 默认预算；截断由集成层短路返回 None → 走 None 降级（不崩溃） |
| 结构化模型键 | "fast" | `plan_model_key` / `summarize_model_key` 构造注入（默认 fast），不进 AgentContext |

## 测试状态

`tests/unit/test_planner.py` + `tests/unit/test_planner_agent.py` + `tests/unit/test_prompts.py`：

- 三阶段全路径：规划 → 2 步执行（第 2 步依赖第 1 步）→ 汇总 structured，`plan` 快照 id/description、usage（react + structured）累计、done 抑制与口径一致
- 降级路径：规划 None → 全量 ReAct 兜底 / AppError → PLAN_FAILED 默认 CONTINUE 兜底 / PLAN_FAILED RAISE 抛 AgentRunError / STOP 硬失败
- 步骤失败 → replan 产修订尾继续执行 + 失败步留审计 + `replan_rounds=1` + 新步 id 续接 max+1 单调重编号
- replan 耗尽（无成功步）→ 纯失败 partial；汇总 None → 纯文本拼装（有步骤产出部分成功）
- Schema 严格性（四个 schema required + additionalProperties:False，fixture 过 jsonschema）
- 护栏：共享类型化优先级、规划/重规划/汇总调用前后复查、子 ReAct 返回后先吸收 usage 与当前步骤再终止、汇总成功后取消仍保留 structured/usage；成本越界不空转 replan/summarize
- 桥接：PlannerAgent 端到端 metadata 映射（plan / steps_executed / replan_rounds / degraded）/ outcome None 兜底 / `ctx.max_refine_rounds` → replan 预算透传

## 设计决策

决策要点均归档 [ADR planner-strategy](../../../adr/domain/reasoning/2026-09-05-planner-strategy.md)（Context → Decision → Consequences）：

- **两层结构分界**：`reasoning/planner.py`（原子策略）+ `agent/planner.py`（编排桥接，生命周期依赖归 agent/）
- **每步复用 `ReActStrategy.execute`** 取代裸 `execute_tool_calls`：步骤级护栏 / 上下文隔离随策略
- **replan 预算复用 `max_refine_rounds` 语义**（与 Reflection 修正共用）
- **新增 `PLAN_FAILED`**（规划 / 重规划 / 汇总失败降级）
- **每步上下文隔离**：`_step_messages` 只带已完成步骤摘要（截断 500/4000）
- **规划器工具定稿**：tools=None + introspection 工具目录（最小权限）

## 问题记录

- [REASON-016 跨策略执行护栏](../../../issues/domain/reasoning/2026-09-10-cross-strategy-guard-priority.md)：结构化调用前后统一复查；子 ReAct 返回后先吸收成果与 usage，再按优先级终止

## 相关文档

- [planner_benchmark.md](planner_benchmark.md)（工业级对标基准）
- [推理策略模块](reasoning.md)（主文档）
- [ReActStrategy 策略组件](react.md)（每步执行器，同级组件）
- [ReflectionStrategy 策略组件](reflection.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 PlannerAgent 桥接）
- [领域层说明](../README.md)
