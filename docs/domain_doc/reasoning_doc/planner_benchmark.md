# Planner 策略工业级对标（完成度基准）

> **对象**：`app/domain/reasoning/planner.py`（PlannerStrategy）
> **更新日期**：2026-09-05
> **定位**：Planner 推理策略（Plan-then-Execute 单 Agent 编排）的工业级能力基准与差距清单——供策略增强、实现评价参考
> **依据**：Plan-and-Execute 论文 / LLMCompiler（DAG 并行编排）/ MetaGPT（SOP 角色流水线）/ 结构化规划优先与最小权限工具原则的工业级实践（详见 [ADR planner-strategy](../../../adr/domain/reasoning/2026-09-05-planner-strategy.md)「工业级参照」节）

---

## 📋 目录

- [Planner 策略工业级对标（完成度基准）](#planner-策略工业级对标完成度基准)
  - [📋 目录](#-目录)
  - [结论先行](#结论先行)
  - [工业级参照：Planner 能力清单](#工业级参照planner-能力清单)
  - [本项目逐项对照](#本项目逐项对照)
  - [取舍表（刻意不做）](#取舍表刻意不做)
  - [产品导向视角](#产品导向视角)
  - [相关文档](#相关文档)

---

## 结论先行

- **两种「规划-执行」形态必须分清**：`Plan-and-Execute`（规划器产目标式步骤，执行器逐步骤跑 agent 循环）+ `LLMCompiler`（规划器产依赖 DAG，任务并行调度）。本项目落地 = Plan-and-Execute 单 Agent 串行形态（`PlannerStrategy` 三阶段），DAG 并行调度语义对应 Phase C Orchestrator（多 Agent 并行排查），不在本模块复制。
- **工业级共同主线**：**规划与执行是不同认知任务，规划器产结构化计划、执行器逐「步」跑 agent 循环；结构化规划（JSON Schema 产出）优于自由文本解析；规划器只做计划不做执行（最小权限）。**
- **取舍自洽**：串行单 Agent（depends_on 顺序纪律断言）对齐「主 Agent 拆分 → 并行子 Agent 排查」主链路——单 Agent 层不做并行 DAG、不做每步 replan、不做多规划器 SOP，能力边界与产品阶段匹配（见「取舍表」）。
- **ReAct 复用是核心杠杆**：每步执行复用 `ReActStrategy.execute`（被步骤约束的 agent 循环），全套护栏（错误分发 / 停滞 / cancel / 超时 / 上下文预算 / cost_limiter）+ `ReActOutcome` 统一归并——执行器不重造工具循环（比多数工业框架自实现 executor 更收敛）。

## 工业级参照：Planner 能力清单

> 工业级「规划-执行」不是「先让 LLM 写个步骤清单再逐条提示」，而是围绕规划 / 执行 / 重规划 / 收敛的工程护栏。五参照要点：

| 参照 | 核心形态 | 本项目对应 |
| --- | --- | --- |
| **Plan-and-Execute** | 规划器产出目标式步骤清单（goal + steps），执行器逐「步」调用 LLM 完成该步 | 三阶段分离 + 每步 `ReActStrategy.execute` |
| **LLMCompiler DAG** | 规划器产出带依赖的 DAG，`depends_on` 驱动并行任务调度（可并行任务同时跑） | `depends_on` 仅作顺序纪律断言；并行调度留 Phase C Orchestrator |
| **MetaGPT SOP** | 多角色流水线（PM/架构师/工程师各司其职），单一 Agent 内不展开 | 单 Agent 串行；多 Agent 编排留 Phase C |
| **结构化规划优先** | 计划经受约束的结构化产出（schema 校验），弃自由文本步骤解析 | `PLAN_SCHEMA` / `REPLAN_SCHEMA` JSON Schema（required + additionalProperties:False） |
| **最小权限 introspection 工具目录** | 规划器「知道工具可指名、不持有调用权」，工具权只在执行器 | `_tool_catalog` 文本目录注入规划 prompt；`generate_structured` 恒不传 tools → 无真 tool_calls |

## 本项目逐项对照

> 状态标注：✅ 完备 ｜ ⚠️ 部分 / 有隐患 ｜ ❌ 缺失。实现文件 `app/domain/reasoning/planner.py`。

| # | 特性 | 状态 | 本项目实现 |
| --- | --- | --- | --- |
| 1 | 三阶段显式分离（规划 → 执行 → 汇总） | ✅ | `execute()` 分阶段（A 规划 / B 执行 / C 汇总），独立私有方法（`_plan` / `_replan` / `_summarize`），非单一 prompt |
| 2 | 规划器只产计划不执行 | ✅ | `_plan` / `_replan` 仅 `generate_structured`；工具调用权只在每步执行器（ReAct） |
| 3 | 执行器逐「步」跑 agent 循环 | ✅ | 每步复用 `ReActStrategy.execute(step.description, 步隔离消息)`——被步骤约束的 ReAct 小跑，护栏全套免费 |
| 4 | 步骤自包含 + depends_on | ✅ | `PLAN_STEP_SCHEMA`：description 目标式单步可独立完成 + depends_on 必填（防跳过排序思考） |
| 5 | 依赖纪律断言 | ✅ | `_normalize_steps` 程序化单调 id + 丢弃自引/前瞻/未知引用；列表序即合法拓扑序 |
| 6 | 重规划（失败恢复） | ✅ | 仅步骤失败触发 `_replan_loop`（≤ max_replan_rounds）；基于已完成步骤产新尾替换未执行部分，绝不重复已完成工作 |
| 7 | 重规划 id 续接单调 | ✅ | 新尾 id = max(executed.id)+1 起单调重编号（防依赖错位） |
| 8 | 结构化最终输出（证据链报告） | ✅ | `RESULT_SCHEMA`：summary + conclusions（claim/evidence/confidence）+ explicit_abstention——对齐产品主链路报告规范 |
| 9 | 失败降级（best-effort） | ✅ | 规划失败→全量 ReAct 兜底；replan 耗尽→部分汇总/纯失败；汇总 None→纯文本拼装；全部 degraded=True 不抛错 |
| 10 | token/成本统计 + 预算护栏 | ✅ | react 各步 + plan/replan/summarize 结构化全阶段累计到 outcome；cost_limiter 每阶段发起付费调用前 check（超限停机）；`max_execution_time` 全局墙钟每步转剩余 |
| 11 | 事件 / 可观测性 | ✅ | 阶段 info + 步骤 tool 事件透传 + 收尾 done（抑制每步 ReAct 中间 done，口径对齐 outcome） |
| 12 | 终止护栏（cancel / 超时 / 成本） | ✅ | 每阶段顶部 + 每步前检查；cancel 在步骤 react 中置位 → 立即终止（防误判步骤失败而 replan） |
| 13 | 上下文隔离（跨步不膨胀） | ✅ | `_step_messages` 只带已完成步骤摘要（`_executed_summary` 截断 500/4000），不累积原始工具 transcript |

## 取舍表（刻意不做）

| 取舍 | 状态 | 理由 / 落地时机 |
| --- | --- | --- |
| 并行 DAG 调度 | ❌ 刻意不做 | LLMCompiler 式任务并行属**多 Agent 并行排查**，是 Phase C Orchestrator 职责（主链路「拆分 → 并行子 Agent 排查」）；单 Agent 层串行保正确性优先，出现真实主链路需求再落地 |
| 每步 replan | ❌ 刻意不做 | 仅**步骤失败**触发重规划（限 max_replan_rounds），不做每步自适应 replan——replan 是有代价的修复调用，成功路径不重复付费（工业「cap + 显式终止」共识） |
| 多 Agent / SOP 流水线 | ❌ 刻意不做 | MetaGPT 式多角色分工是 Phase C 多 Agent 编排；PlannerStrategy 定位单 Agent 编排原语 |
| 自由文本计划解析 | ❌ 刻意不做 | 结构化规划优先：计划经 JSON Schema 产出 + 程序校验，弃文本解析（步骤形状 / depends_on / 上限程序化强制） |
| 规划器真调工具 | ❌ 刻意不做 | 最小权限：规划器只 introspection（文本目录可指名），不持有调用权——规划失败不产生任何工具副作用 |

> 重新评估时机：Phase C Orchestrator 多 Agent 编排落地后——若需**单 Agent 内**并行步骤才把 DAG 引入本模块；否则维持串行。

## 产品导向视角

按第一硬性要求「服务主链路（拆分 → 并行排查 → 证据链报告）」检验：

- **Planner = 主 Agent 拆分原语**：主链路第一步是「主 Agent 把良率异常拆成可排查步骤」——`PlannerStrategy` 产出 goal + 依赖步骤（结构化），作为 Orchestrator 拆分的单 Agent 语义底座。
- **每步复用 ReAct = 子 Agent 排查引擎同款**：Phase C 子 Agent 用 `ReActStrategy` 排查；本模块每步执行就是同一引擎的受控小跑——拆分后每步排查能力与子 Agent 一致，链路贯通。
- **`RESULT_SCHEMA` 对齐证据链报告**：结构化最终输出（结论 + 证据 + 置信度 + 显式放弃）直接服务产品「证据链 + 置信度分级 + 显式放弃」卖点。
- **不并行是当前正确决策**：单 Agent 本地场景先跑通正确性；并行排查的价值在 Phase C 多 Agent 编排释放，不为纯技术前瞻买单。

---

## 相关文档

- [PlannerStrategy 策略组件](planner.md)（本模块对外接口）
- [推理策略模块](reasoning.md)（主文档）
- [ReActStrategy 策略组件](react.md) + [react_benchmark.md](react_benchmark.md)（每步执行器，同级对标）
- [ReflectionStrategy 策略组件](reflection.md) + [reflection_benchmark.md](reflection_benchmark.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 PlannerAgent 桥接）
- [ADR planner-strategy](../../../adr/domain/reasoning/2026-09-05-planner-strategy.md)
