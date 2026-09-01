# Reflection 策略工业级对标（完成度基准）

> **对象**：`app/domain/reasoning/reflection.py`（ReflectionStrategy）
> **更新日期**：2026-09-02
> **定位**：Reflection 推理策略（生成 → 自查 → 修正）的工业级能力基准与差距清单——供策略增强、实现评价参考
> **依据**：Reflexion（NeurIPS 2023）/ Self-Refine（ICLR 2023）/ Huang et al.（ICLR 2024 反证）/ LangChain / LangGraph / OpenAI Agents SDK / SMOLagents / Claude Agent SDK / DSPy / CrewAI 源码级调研（2026-08-31）

---

## 📋 目录

- [Reflection 策略工业级对标（完成度基准）](#reflection-策略工业级对标完成度基准)
  - [📋 目录](#-目录)
  - [结论先行](#结论先行)
  - [工业级参照：Reflection 能力清单](#工业级参照reflection-能力清单)
    - [核心必备（12 项）—— 缺失会损害正确性 / 可观测性 / 稳定性](#核心必备12-项-缺失会损害正确性--可观测性--稳定性)
    - [增强项（10 项）—— 特定场景（长任务 / 多 Agent / HITL）才需要](#增强项10-项-特定场景长任务--多-agent--hitl才需要)
  - [本项目逐项对照](#本项目逐项对照)
    - [核心必备对照](#核心必备对照)
    - [增强项对照](#增强项对照)
  - [未实现增强项的实现时机](#未实现增强项的实现时机)
  - [完成度评估遗留问题收尾](#完成度评估遗留问题收尾)
  - [产品导向视角](#产品导向视角)
  - [相关文档](#相关文档)

---

## 结论先行

- **三种「Reflection」必须分清**：`Reflexion`（跨轮次，反思存长期记忆）+ `Self-Refine`（同轮内迭代）+ `框架 Reflection`（generate→critique→revise 图循环）。本项目落地形态 = 框架 Reflection（单次报告内的自查修正），Reflexion 的跨轮次记忆对应 MemoryService 预留。
- **工业级共同红线（Huang et al. 反证）**：**纯内在自查（intrinsic）不可靠，critic 必须被外部信号（工具结果 / 证据链）锚定**——本项目以 Grounding 为核心（自查注入证据链记录），满足该红线。
- **核心必备 12 项全部落地**（逐项对照见下），成本统计与护栏（#11）已通过 `generate_structured(usage=...)` 回填全阶段覆盖。
- **增强项取舍**：异模型 critic（构造注入入口）✅ / 停滞检测（复用 ReAct）✅；跨轮次记忆、多 critic、HITL、回流微调按「不做或预留」降级（见产品导向视角）。

## 工业级参照：Reflection 能力清单

> 工业级 Reflection 不是「多调一次 LLM」，而是围绕四阶段（生成 / 自查 / 修正 / 终止）的工程护栏。共同主线：**生成与评判是不同认知任务，必须显式分离；critic 必须 grounded（被外部信号锚定），否则内在自查已被证伪**（Huang et al.）。

### 核心必备（12 项）—— 缺失会损害正确性 / 可观测性 / 稳定性

| # | 特性 | 工业级普遍做法 | 代表实现 |
| --- | --- | --- | --- |
| 1 | 三阶段显式分离 | 生成与评判独立为节点/方法，禁止「边写边评」混在单一 prompt | LangGraph generate/reflect 节点；Reflexion Actor/Revisor |
| 2 | Critic 上下文隔离 | 独立 critic 节点/上下文，可异模型，打破同模型共享盲区 | LangGraph reflect 节点；Claude 独立 subagent |
| 3 | **Grounding（证据锚定）** | critic 被外部信号锚定（工具结果/检索/ground truth），杜绝纯 intrinsic | Reflexion 强制 citations；CRITIC 工具交互 |
| 4 | 结构化 Critique 输出 | issues 清单 / rubric 评分 + rationale；自由文本批评无法保证改进 | Reflexion `{critique}` schema；LLM-as-judge rubric |
| 5 | 迭代上限 | 1–3 轮 + 硬上限，防止无限改 | LangGraph MAX_ITERATIONS；Taskade cap 2–3 |
| 6 | 终止路由 | 显式条件边/终止判定，非死循环 | LangGraph should_continue |
| 7 | issues 回喂 | critique 以独立消息/注入 revise prompt 回喂 generator | LangGraph critique→HumanMessage |
| 8 | 完整上下文修正（ground-truth 兜底） | 基于完整上下文重写，不盲目采纳 issues | Reflexion 修正锚证据 |
| 9 | 失败降级（best-effort） | 返回 best/last 输出而非抛错 | LangChain「固定 N 步返回」 |
| 10 | 结构化初稿（面向证据） | 初稿带工具证据 + schema 校验，非空写 | LangChain Reflexion validator |
| 11 | token/成本统计 + 预算护栏 | 每轮 usage 累计 + 成本上限 | CostLimiterPort（本项目） |
| 12 | 事件 / 可观测性 | 每阶段可观测事件，critique 内容可见可追溯 | SSE 事件体系（本项目） |

### 增强项（10 项）—— 特定场景（长任务 / 多 Agent / HITL）才需要

| # | 特性 | 工业级普遍做法 | 代表实现 |
| --- | --- | --- | --- |
| 13 | 跨轮次反思记忆（episodic memory） | bounded buffer 长期记忆，跨任务改进 | Reflexion episodic memory；MemoryService |
| 14 | 异模型 critic | critic 用不同/更强模型，成本分级 + 破盲区 | Claude Haiku critic vs Opus 生成 |
| 15 | 执行/测试回填 | critic 跑真实校验（代码/测试/查询）而非纯 LLM 判断 | SMOL 执行反馈；Reflexion 单测 |
| 16 | 多 critic/多 Agent 对抗 | generator/critic 目标对立，critic 见外部数据 | TowardsAI 多阶段多 agent |
| 17 | 最终答案校验 | 反射后产物经确定性断言才接受 | SMOL final_answer_checks |
| 18 | 修正策略可配置（patch vs 重写） | 局部 patch vs 完整重写可切换 | Learning-to-repair 代码分支 |
| 19 | 反思回流微调 | reflection 作为训练数据 | LangChain fine-tuning data |
| 20 | HITL 人工审批 | critic/修正经人工确认 | CrewAI verifier 角色 |
| 21 | 自适应触发 | 按置信度/难度决定是否反思 | DSPy 分级 |
| 22 | 循环停滞检测（反思死循环） | 相同 critique/答案不再变化判停滞 | SMOL same_action_llm_turn_limit |

---

## 本项目逐项对照

> 状态标注：✅ 完备 ｜ ⚠️ 部分 / 有隐患 ｜ ❌ 缺失。实现文件 `app/domain/reasoning/reflection.py`。

### 核心必备对照

| # | 特性 | 状态 | 本项目实现 |
| --- | --- | --- | --- |
| 1 | 三阶段显式分离 | ✅ | `execute()` 分阶段（收集+初稿 → 自查 → 修正），独立私有方法（`_generate_critique` / `_generate_refine`），非单一 prompt（[reflection.py](../../../app/domain/reasoning/reflection.py)） |
| 2 | Critic 上下文隔离 | ✅ | 自查消息 = 独立 LLM 调用（证据链记录 + 初稿），与生成循环中间推理隔离；`critique_model_key` 可注入异模型（增强 #14 入口） |
| 3 | Grounding（证据锚定） | ✅ | `CRITIQUE_PROMPT` 注入证据链记录，grounding 为自查第 1 维度——每条 claim 的 supporting_evidence 必须引用真实工具记录 |
| 4 | 结构化 Critique 输出 | ✅ | `CRITIQUE_SCHEMA`：ok + issues[]（severity / dimension 9 枚举 / claim / description） |
| 5 | 迭代上限 | ✅ | `max_refine_rounds`（默认 2 = 初稿 + 至多 1 修正），**真迭代**：修正后复查（ok 即停 early exit / 新 issues 再修正），cap + early exit，落在工业 1–3 轮 |
| 6 | 终止路由 | ✅ | 显式降级路由表（react 失败 / 无 structured / 自查 ok / issues / 修正失败 / 达上限 → 各终止路径） |
| 7 | issues 回喂 | ✅ | `REFINE_PROMPT` 注入 draft + issues（结构化 issues 作为受控参数） |
| 8 | 完整上下文修正 | ✅ | `REFINE_PROMPT` 要求完整重写（非 patch）+ 逐条判断 issues（与证据链矛盾的不采纳，以证据链为准） |
| 9 | 失败降级（best-effort） | ✅ | 自查/修正失败/ReAct 无 structured 全部 → 采用最近稿（degraded=True），不抛错；捕获范围覆盖 AppError 全家族（拒答/工具调用/熔断），非 AppError 编程错误仍冒泡（[REASON-010](../../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)）；结构化截断由集成层短路返回 None 走 None 降级 |
| 10 | 结构化初稿（面向证据） | ✅ | 生成阶段复用 `ReActStrategy.execute(output_schema=REFLECTION_SCHEMA)`：工具收集 + final_answer 结构化初稿一次完成 |
| 11 | token/成本统计 + 预算护栏 | ✅ | ReAct 收集阶段经内部 `_react` 的 cost_limiter / context_budget 完整护栏；critique/refine 经 `generate_structured(usage=...)` 回填 token 用量（仿 async_generate result 模式），累计到 outcome.total_tokens/usage + cost_limiter（循环顶部 check，超限停机降级）——全阶段覆盖 |
| 12 | 事件 / 可观测性 | ✅ | ReAct 事件零改动 + 阶段 info（进入自查/自查通过/发现 N 问题/降级）+ done |

### 增强项对照

| # | 特性 | 状态 | 备注 |
| --- | --- | --- | --- |
| 13 | 跨轮次反思记忆 | ❌ | MemoryService 预留（Phase C 记忆模块），Reflexion 式 episodic memory 升级路径 |
| 14 | 异模型 critic | ✅ | `critique_model_key` 构造注入（默认 "fast"），可换更强/不同模型破盲区 |
| 15 | 执行/测试回填 | ⚠️ | critic 基于收集阶段的工具记录（执行反馈已 grounding）；critic 本身不再次跑工具（RCA 场景 critic 是纯审查，无需再执行） |
| 16 | 多 critic 对抗 | ❌ | 单 critic 自查；多 Agent 对抗留 Phase C 编排 |
| 17 | 最终答案校验 | ⚠️ | 修正后已复查（真迭代，[REASON-009](../../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md)）；最终答案的确定性断言校验（final_answer_checks）留证据链报告产物层 |
| 18 | 修正策略可配置 | ⚠️ | 默认完整重写（工业默认）；patch 不提供（RCA 证据链需完整重写 + 重新锚定） |
| 19 | 反思回流微调 | ❌ | 当前单用户本地场景不服务 |
| 20 | HITL 人工审批 | ❌ | 工具层已有 ApprovalGate 审批；反思环节 HITL 留 Phase D |
| 21 | 自适应触发 | ❌ | 当前 ReflectionAgent 显式触发；按置信度跳过低风险任务留后续 |
| 22 | 循环停滞检测 | ✅ | 复用 ReAct 收集阶段内建 STALLED（`max_same_action_turns`）——反思死循环同样被覆盖 |

---

## 未实现增强项的实现时机

> 5 项未实现增强项（#13 / #16 / #19 / #20 / #21）的共同特征：依赖 Phase C/D 主链路能力 + 真实运行数据，是「主链路先跑通、再用评测数据驱动后置决策」的增强项——当前均维持预留（不做或预留原则），不进入当前路线图。实现次序按产品主链路关联度排序：

| # | 增强项 | 实现时机 | 触发信号 | 前置依赖 |
| --- | --- | --- | --- | --- |
| 21 | 自适应触发 | 主链路跑通 + 评测对比显示低难度任务反思 ROI 为负（成本优化项，成本最低最先做） | 反思额外调用成为可感知成本，且数据证明低收益 | 评测体系；`REFLECTION_SCHEMA.confidence`（0~1）已就位，阈值判定薄逻辑 |
| 13 | 跨轮次反思记忆 | Phase D 记忆模块（短期注入 ContextManager + 长期向量 RAG）落地时顺带 | 同类良率问题反复出现，需复用上次排查路径 | MemoryService + VectorStorePort + EmbeddingPort |
| 20 | HITL 人工审批 | 产品多人/企业部署、报告需生效/驳回流程约束时 | 单用户本地走向部署（JWT/auth 真实落地） | auth 鉴权；工具层 ApprovalGate 已有，反思层独立建设 |
| 16 | 多 critic 对抗 | 评测数据指出证据链误判/漏判率不可接受且归因「自查盲区」时 | 单 critic 自查成为误判主要来源（成本最重，无数据不做） | Phase C Orchestrator（对抗 agent 属编排层能力） |
| 19 | 反思回流微调 | 数据量可训练 + 微调管线 + 评测验证收益三者同时成熟 | 真实运行数据达到训练规模 | eval 评测体系 + 数据管道 + 模型微调管线 |

### 部分实现项（⚠️）

- **#15 执行/测试回填**：critic 基于收集阶段的工具记录（执行反馈已 grounding），不再跑工具——RCA 场景 critic 是纯审查
- **#17 最终答案校验**：修正后已复查（真迭代）；确定性断言校验（final_answer_checks）留证据链报告产物层
- **#18 修正策略可配置**：默认完整重写（RCA 证据链正确默认）；patch 不提供，留升级路径

按「出现真实需求再落地」。

### 判断框架

- **重新评估时机**：主链路跑通——Phase C Orchestrator 多 Agent 编排 → Phase D Yield RCA 证据链闭环 + 评测体系就位
- **触发依据**：评测数据指出具体短板——成本 → #21 · 跨任务复用 → #13 · 报告误判 → #16 · 流程审批 → #20 · 模型能力 → #19
- **单一触发点**：任一项满足「有真实消费方 + 有数据支撑 + 依赖就位」即单独落地，不等整组

---

## 完成度评估遗留问题收尾

> 2026-09-01 工业级完成度评审发现 4 类缺口（P1~P4），均已修复收口；问题生命周期见各 issue，评审要点与修复细节见 [reflection.md](reflection.md)。

| # | 问题 | 修复 | 状态 |
| --- | --- | --- | --- |
| **P1** | 自查/修正异常捕获只覆盖 `StructuredRefusalError`/`ToolCallError`，熔断等不可恢复 AppError 冒泡崩溃（降级保证不完整） | `_critique`/`_refine` 改 `except AppError` 统一捕获 + 集成层 openai 不可恢复异常归一 `LLMAPIError`（AppError 树）→ 领域层统一兜底 | ✅ [REASON-010](../../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md) |
| **P2** | done 事件 total_tokens 只报 react 收集阶段，漏计 critique/refine 用量（SSE 事件与 outcome 事实源漂移，成本审计失真） | `_finalize` done 事件口径与 outcome 一致（react + 结构化累计）+ 抑制 ReAct 中间 done（事件流仅保留收尾 1 个） | ✅ [REASON-011](../../../issues/domain/reasoning/2026-09-01-reflect-done-token-caliber.md) |
| **P3** | 反思循环只有 cost_limiter 一重护栏，cancel / 总时长超限未检查（与 ReAct 收集阶段三重护栏不对称，取消/超时后仍发付费调用） | `_should_abort` 循环顶部检查（用户取消 / `max_execution_time` 超限 → 停机降级采用最近稿） | ✅ 见 [reflection.md](reflection.md) 边界情况 |
| **P4** | 次要项：`explicit_abstention` 可选（产品「显式放弃」软契约）· 实例非协程安全未声明 · critique/refine 未显式传 max_tokens | P4-2 `required` 加 `explicit_abstention`（程序化强制）· P4-3 docstring 声明实例单次执行 · P4-1 评估非缺口（默认预算 `llm_structured_max_tokens` + 截断降级兜底），文档说明 | ✅ 见 [reflection.md](reflection.md) 边界情况 |

---

## 产品导向视角

按第一硬性要求「服务主链路（拆分 → 并行排查 → 证据链报告）」检验：

- **Reflection = 证据链报告的语义自查（防伪证据）**：程序校验（jsonschema）查形状，模型自查查语义——良率根因报告结论必须可回溯、无编造、置信度分级、证据不足显式放弃。
- **Grounding 是核心价值**：`CRITIQUE_PROMPT` 注入证据链记录，critic 只核对「结论是否与工具数据一致 / 每条 claim 是否可回溯」——直接踩证据链产品亮点（product.md「证据链 + 置信度分级 + 显式放弃」）。
- **`explicit_abstention` 对齐产品「显式放弃」**：证据不足时 critic 的 evidence_gap 维度抓出 → 修正移入 explicit_abstention——比通用 Reflection 的「改到通过」更符合良率工程师真实需求（明确「不能下结论 + 建议补充什么数据」）。
- **增强项（跨轮次记忆 / 多 critic / HITL / 回流微调）不服务当前单用户本地场景，当前不做是对的**——符合项目「不做或预留」降级原则。

---

## 相关文档

- [ReflectionStrategy 策略组件](reflection.md)（本模块对外接口）
- [推理策略模块](reasoning.md)（主文档）
- [ReActStrategy 策略组件](react.md) + [react_benchmark.md](react_benchmark.md)（同级组件与对标）
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 ReflectionAgent 桥接）
- [ADR reflection-strategy](../../../adr/domain/reasoning/2026-08-31-reflection-strategy.md)
- [问题记录 REASON-009](../../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md)（修正循环真迭代）
