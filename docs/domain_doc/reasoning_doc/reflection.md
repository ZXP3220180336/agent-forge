# ReflectionStrategy 设计文档

> **模块**：`app/domain/reasoning/reflection.py`
> **更新日期**：2026-08-31
> **职责**：Reflection 原子推理策略——生成 → 自查 → 修正三阶段，模型自我评估输出质量并改进
> **状态**：✅ 已实现
> **配套**：桥接见 [agent/reflection.py](../agent_doc/agent.md)；工业级对标见 [reflection_benchmark.md](reflection_benchmark.md)

---

## 📋 目录

- [ReflectionStrategy 设计文档](#reflectionstrategy-设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [三阶段显式分离](#三阶段显式分离)
    - [Grounding（证据锚定）](#grounding证据锚定)
    - [自查清单维度（Scope 盲区教训）](#自查清单维度scope-盲区教训)
    - [降级路由（best-effort）](#降级路由best-effort)
    - [CRITIQUE\_FAILED 错误分发](#critique_failed-错误分发)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [ReflectionStrategy](#reflectionstrategy)
    - [ReflectionOutcome（结果载体）](#reflectionoutcome结果载体)
    - [Schema 契约](#schema-契约)
  - [执行流程](#执行流程)
  - [对外接口](#对外接口)
  - [边界情况](#边界情况)
  - [配置项清单](#配置项清单)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [相关文档](#相关文档)

---

## 设计目标

1. **三阶段显式分离**：生成（收集+初稿）→ 自查（Critic）→ 修正（Refine）独立方法——生成与评判是不同认知任务，禁止混在单一 prompt（工业基准 #1）
2. **Grounding 为核心**：Critic 必须对照工具记录（证据链）自查，杜绝纯内在自查（Huang et al. 反证：内在自查会让模型把对的改错）——每条结论可回溯工具记录（工业基准 #3）
3. **复用 ReAct 收集**：生成阶段复用 `ReActStrategy.execute(output_schema=...)`——工具收集 + final_answer 结构化初稿一次完成（Reflexion「生成即工具增强」），证据链在 `outcome.tool_calls`
4. **失败降级不抛错**：自查 / 修正失败采用最近稿（degraded=True），返回 best-effort（工业基准 #9）
5. **纯算法依赖方向**：只依赖 ports + shared + prompts（指令层），收标量参数——可独立测试、被 agent/ 编排调用

## 核心概念解释

### 三阶段显式分离

| 阶段 | 职责 | 复用/调用 |
| --- | --- | --- |
| 生成 | ReAct 收集工具证据 + final_answer 提交结构化初稿 | `ReActStrategy.execute(output_schema=REFLECTION_SCHEMA)` |
| 自查 | Critic 对照证据链审查初稿，产出结构化自查报告 | `generate_structured(CRITIQUE_SCHEMA)` |
| 修正 | 基于证据链 + 审查意见完整重写（ground-truth 兜底） | `generate_structured(REFLECTION_SCHEMA)` |

### Grounding（证据锚定）

Critic 被外部信号（工具记录）锚定：自查消息注入「证据链记录 + 初稿 JSON」，Critic 只核对「结论是否与工具数据一致 / 每条 claim 是否可回溯」。杜绝「两个智能鹦鹉互相夸」的内在自查。

### 自查清单维度（Scope 盲区教训）

自查范围 = 提示词清单，清单外必漏（learning-agent step3 实测）。`CRITIQUE_PROMPT` 穷举 9 个 dimension（`CRITIQUE_SCHEMA.dimension` enum 同步）：grounding / consistency_with_data / fabrication / attribution / confidence_calibration / evidence_gap / completeness / internal_consistency / next_steps_actionable。模板明确「本清单为唯一自查范围」——把盲区变成显式契约。

### 降级路由（best-effort）

| 情形 | 处理 |
| --- | --- |
| ReAct 失败 | 降级 outcome（error 透传，证据链保留），degraded=True |
| ReAct 无 structured（未调 final_answer） | 降级（content 保留），degraded=True |
| 自查失败 / 拒答 | CRITIQUE_FAILED 分发，默认采用 draft，degraded=True |
| 自查 ok | 采用 draft（degraded=False） |
| 自查 issues + 修正 + 复查 ok | 采用 refined（degraded=False） |
| 修正失败 / 达上限（复查未通过） | 采用最近稿，degraded=True |

### CRITIQUE_FAILED 错误分发

自查 / 修正失败统一走 `AgentErrorKind.CRITIQUE_FAILED`（默认 CONTINUE）——独立 kind 而非复用 `STRUCTURED_INVALID`（语义不符）或 `REFUSED`（默认 STOP 会硬停整个 Agent）。动作：CONTINUE=降级采用 best（默认）/ STOP=整个失败 / RAISE=抛 `AgentRunError`。

## 架构总览

```text
ReflectionAgent._strategy_cycle()（agent/ 层编排：生命周期 / 状态 / 结果组装）
        ▼ execute()
ReflectionStrategy.execute()（三阶段）
    ├── 阶段一：ReActStrategy.execute(output_schema=REFLECTION_SCHEMA)
    │       ├── 工具收集（证据链 → outcome.tool_calls）
    │       └── final_answer 结构化初稿（→ outcome.structured）
    ├── 阶段二：generate_structured(CRITIQUE_PROMPT + 证据链 + draft, CRITIQUE_SCHEMA)
    └── 阶段三：generate_structured(REFINE_PROMPT + 证据链 + draft + issues, REFLECTION_SCHEMA)
        └── 错误分发：ErrorHandlerRegistry（CRITIQUE_FAILED，default CONTINUE）
```

**依赖方向**：`reasoning → ports + shared + prompts`（不 import agent/）；被 agent/reflection.py 编排调用。

## 组件详解

### ReflectionStrategy

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(llm, tools, context_budget=None, error_handlers=None, cost_limiter=None, output_schema=None, critique_schema=None, critique_model_key="fast")` | 构造 `_react = ReActStrategy(...)`（护栏透传）；schema 可注入覆盖 |
| `execute` | `(user_input, messages, *, max_iterations, temperature, max_tokens, max_execution_time=None, max_context_rounds=None, max_context_tokens=None, max_empty_retries=2, max_llm_fail_retries=2, max_same_action_turns=3, tool_timeout=None, tool_max_retries=None, max_refine_rounds=2, cancel_event=None)` | 三阶段主流程；yield SSE 事件，结果写入 `outcome` |

### ReflectionOutcome（结果载体）

| 字段 | 说明 |
| --- | --- |
| `content` / `reasoning` | 自由文本（ReAct 降级路径保留） |
| `structured` | 最终结构化（refine 后取 refine 稿；否则 draft） |
| `draft` / `critique` | 初稿 / 自查结果（供审计） |
| `refine_rounds` / `degraded` | 实际修正轮数 / 是否经降级路径 |
| `tool_calls` | 证据链（来自 ReAct outcome） |
| `iterations` / `total_tokens` / `usage` | 统计（ReAct 累计；critique/refine 无 usage 回传，见 benchmark ⚠️） |
| `error` / `success` | 失败原因 / 是否成功 |

### Schema 契约

- `REFLECTION_SCHEMA`（初稿）：summary / conclusions[]（claim + supporting_evidence[] + confidence 0~1）/ next_steps[] / assumptions[] / explicit_abstention[]，required=[summary, conclusions, next_steps]
- `CRITIQUE_SCHEMA`（自查）：ok / issues[]（severity + dimension 9 枚举 + claim + description），required=[ok]

## 执行流程

```text
阶段一 收集+初稿：ReAct.execute(output_schema=REFLECTION_SCHEMA)
  ├─ react 失败 → 降级 outcome（error 透传）
  ├─ react 无 structured → 降级（content 保留）
  └─ draft = outcome.structured；evidence = outcome.tool_calls（序列化剔除 final_answer）
阶段二+三 自查 → 修正 → 复查 循环（真迭代，每次修正后重新自查）：
  current = draft
  while True:
    ├─ 自查 current（CRITIQUE_SCHEMA）→ 失败 → 降级采用 current（degraded）
    ├─ critique.ok → 采用 current（degraded=False，early exit）
    ├─ refine_round >= max_refine_rounds-1 → 达上限，采用 current（degraded）
    └─ issues → 修正（REFINE_PROMPT + 证据链 + current + issues）→
        成功 current = refined → 回到循环顶部重新自查修正稿
```

> 真迭代依据（工业标准）：Self-Refine 每轮用新 feedback；LangGraph「revise 后必 re-reflect，否则循环无效」。复用同一批 issues 反复修正是反模式（[REASON-009](../../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md)）。

## 对外接口

> 对外接口 = 被 agent/ 层编排依赖的策略类。`ReflectionAgent._strategy_cycle()` 委托 `ReflectionStrategy.execute()`。

| 策略类 | 契约（方法） | 说明 |
| --- | --- | --- |
| `ReflectionStrategy` | `execute(...)` + `outcome` | 完整契约见上文；被 ReflectionAgent 编排 |

## 边界情况

| 场景 | 处理 |
| --- | --- |
| 模型未调用 final_answer（stop 自由文本） | 降级：content 保留、structured=None、degraded=True |
| ReAct 失败（LLM 错误等） | error 透传、证据链保留、degraded=True |
| 自查返回 None / 拒答 | CRITIQUE_FAILED 分发 → 默认采用 draft（degraded=True） |
| 修正返回 None / 达 max_refine_rounds 上限 | 采用最近稿（degraded=True） |
| 证据链含 final_answer 条目 | 序列化时剔除（终止工具非真实证据） |
| 证据链为空 + draft 引用不存在证据 | 自查 grounding 维度抓出（Grounding 价值） |

## 配置项清单

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `agent_max_refine_rounds` | int | 2 | Reflection 报告生成最大尝试轮数（初稿 1 + 至多 N-1 次修正）；经 `AgentContext.max_refine_rounds` 注入 |

## 测试状态

`tests/unit/test_reflection.py`（15 用例）+ `tests/unit/test_reflection_agent.py`（5 用例）：

- 三阶段全路径：自查 ok 采用初稿 / 自查 issues 修正 + 复查 ok 采用 refined
- 真迭代：修正后复查新 issues 再修正（refine_rounds=2）/ 达上限采用最后修正稿（degraded）
- Grounding 注入断言（自查入参含证据链 + 初稿）
- 降级路径：自查失败 / 拒答 / 修正失败 / ReAct 无 structured / ReAct 失败
- max_refine_rounds 上限（max=1 不修正）、CRITIQUE_FAILED 分发（RAISE/STOP）、Schema 校验
- Scope 盲区清单维度穷举（9 dimension）、护栏透传
- 桥接：_map_outcome 映射 / ctx 透传 / 端到端 / 默认向后兼容

## 设计决策

- 三阶段显式分离动机、grounding 反内在自查（Huang et al.）、schema 常量+注入取舍、新增 CRITIQUE_FAILED 理由、降级路由、cost 缺口取舍——见 [ADR reflection-strategy](../../../adr/domain/reasoning/2026-08-31-reflection-strategy.md)

## 相关文档

- [reflection_benchmark.md](reflection_benchmark.md)（工业级对标基准）
- [推理策略模块](reasoning.md)（主文档）
- [ReActStrategy 策略组件](react.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 ReflectionAgent 桥接）
- [领域层说明](../README.md)
