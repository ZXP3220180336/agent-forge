# ReflectionStrategy 设计文档

> **模块**：`app/domain/reasoning/reflection.py`
> **更新日期**：2026-09-16
> **职责**：Reflection 原子推理策略——生成 → 自查 → 修正三阶段，模型自我评估输出质量并改进
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
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
    - [阶段性语义上下文缩减](#阶段性语义上下文缩减)
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
  - [问题记录](#问题记录)
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

### 阶段性语义上下文缩减

自查和修正的单条 user 消息同样受 `max_context_tokens` 约束。`ContextBudgetPort.count_tokens` 只提供统一计量，Reflection 的字段取舍由 Prompt 模块内部的 `_reflection_payload` 组件完成，`PromptManager` 保持公开组装入口：critique 动态区按 evidence 60% / draft 40%，refine 按 evidence 45% / draft 35% / issues 20% 初分配，未使用额度按声明顺序回流。

证据优先保留当前稿引用的记录，再保留最近记录；稳定 `E####` 编号、工具与参数、成功状态、错误码、量测值和时间锚点优先于长结果正文。稿件优先保留结论、证据引用、置信度和 `explicit_abstention`；审查意见优先 critical，再保留较新的 minor。所有删减都有带数量的 `<omitted .../>` 标记。

缩减结果只是下一次请求的只读视图。`react_outcome.tool_calls`、原始 draft、current 和完整 critique 不被修改；最近可验证稿始终是最近一次完整解码并接管 usage 的 current。若最小提示骨架仍超预算，本地零调用并按上下文 Guard 采用 current；若 Integration 最终闸仍拒绝，当前阶段也不做缩减重试。

### 降级路由（best-effort）

| 情形 | 处理 |
| --- | --- |
| ReAct 失败 | 降级 outcome（error 透传，证据链保留），degraded=True |
| ReAct 无 structured（未调 final_answer） | 降级（content 保留），degraded=True |
| 自查普通失败 / 拒答 / 熔断等 AppError | CRITIQUE_FAILED 分发，默认采用 draft，degraded=True |
| 自查无结果且 cancel/deadline/context 命中 | 先按专用 Guard 终态收尾，不改写为 CRITIQUE_FAILED |
| 自查 ok | 成本越界或 graceful cancel 采用 draft；strict deadline 迟到则保留稿与 critique，以 TIMEOUT 部分成果收尾 |
| 自查 issues + 修正 + 复查 ok | 采用 refined（degraded=False） |
| 修正失败 / 达上限（复查未通过） | 采用最近稿，degraded=True |

> 结构化输出截断（StructuredTruncationError）**不在本层缺口内**：`StructuredOutput.extract` 对截断短路返回 `None`（不向上抛，[REASON-010](../../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)），Reflection 走「自查/修正返回 None → 降级」既有路径。

### CRITIQUE_FAILED 错误分发

自查 / 修正的普通 AppError 失败统一走 `AgentErrorKind.CRITIQUE_FAILED`（默认 CONTINUE）。
`ContextWindowExceededError` 是请求 Guard 终态，不进入该分发；调用方先归并本阶段 usage，再按
`CONTEXT_EXCEEDED` 收尾。非 AppError 编程错误不吞、向上冒泡（fail fast，
[REASON-010](../../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)）。

## 架构总览

```text
ReflectionAgent._strategy_cycle()（agent/ 层编排：生命周期 / 状态 / 结果组装）
        ▼ execute()
ReflectionStrategy.execute()（三阶段）
    ├── 阶段一：ReActStrategy.execute(output_schema=REFLECTION_SCHEMA)
    │       ├── 工具收集（证据链 → outcome.tool_calls）
    │       └── final_answer 结构化初稿（→ outcome.structured）
    ├── 阶段二：语义缩减(证据链 + draft) → generate_structured(..., CRITIQUE_SCHEMA)
    └── 阶段三：语义缩减(证据链 + draft + issues) → generate_structured(..., REFLECTION_SCHEMA)
        └── 错误分发：ErrorHandlerRegistry（CRITIQUE_FAILED，default CONTINUE）
```

**依赖方向**：`reasoning → ports + shared + prompts`（不 import agent/）；被 agent/reflection.py 编排调用。

## 组件详解

### ReflectionStrategy

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(llm, tools, context_budget=None, error_handlers=None, cost_limiter=None, output_schema=None, critique_schema=None, critique_model_key="fast")` | 构造 `_react = ReActStrategy(...)`（护栏透传）；schema 可注入覆盖 |
| `execute` | `(user_input, messages, *, max_iterations, temperature, max_tokens, max_execution_time=None, max_context_rounds=None, max_context_tokens=None, max_empty_retries=2, max_llm_fail_retries=2, max_tool_protocol_retries=2, max_same_action_turns=3, tool_timeout=None, tool_max_retries=None, max_refine_rounds=2, cancel_event=None)` | 三阶段主流程；yield SSE 事件，结果写入 `outcome`；协议修正上限透传初稿 ReAct |

内部阶段方法：`_finalize_after_critique` 只在 critique 可用后判定提交当前稿或继续修正，保留
REASON-020 的 after-turn cancel/成本与 strict deadline 差异；`_adopt_refined_draft` 只在修正
返回完整稿时更新最近稿和完成轮数。`_critique`、`_refine`、usage 归并与 `_finalize` 仍由策略持有。

### ReflectionOutcome（结果载体）

| 字段 | 说明 |
| --- | --- |
| `content` / `reasoning` | 自由文本（ReAct 降级路径保留） |
| `structured` | 最终结构化（refine 后取 refine 稿；否则 draft） |
| `draft` / `critique` | 初稿 / 自查结果（供审计） |
| `refine_rounds` / `degraded` | 实际修正轮数 / 是否经降级路径 |
| `tool_calls` | 证据链（来自 ReAct outcome） |
| `iterations` / `total_tokens` / `usage` | 统计（ReAct 收集 + 自查/修正全阶段累计） |
| `error` / `success` | 失败原因 / 是否成功 |

### Schema 契约

- `REFLECTION_SCHEMA`（初稿）：summary / conclusions[]（claim + supporting_evidence[] + confidence 0~1）/ next_steps[] / assumptions[] / explicit_abstention[]，required=[summary, conclusions, next_steps, explicit_abstention]（模型必须产出 explicit_abstention，空数组表示无放弃——产品「证据不足显式放弃」程序化强制，P4）
- `CRITIQUE_SCHEMA`（自查）：ok / issues[]（severity + dimension 9 枚举 + claim + description），required=[ok]

## 执行流程

```text
阶段一 收集+初稿：ReAct.execute(output_schema=REFLECTION_SCHEMA)
  ├─ react 失败 → 降级 outcome（error 透传）
  ├─ react 无 structured → 降级（content 保留）
  └─ draft = outcome.structured；evidence = outcome.tool_calls（critique 序列化时剔除 final_answer 终止条目）
阶段二+三 自查 → 修正 → 复查 循环（真迭代，每次修正后重新自查）：
  current = draft
  while True:
    ├─ 护栏①（自查调用前准入）：cancel > deadline > cost 命中 → 采用最近完整稿降级停机
    ├─ 自查 current（CRITIQUE_SCHEMA）→ 无结果先恢复 cancel/deadline/context，再判普通失败
    ├─ _finalize_after_critique：ok 或达到上限 → strict deadline 复查后提交 current；否则继续
    ├─ 护栏②（修正调用前准入）：命中 → 采用最近稿降级停机，不发起修正
    └─ issues → 修正（REFINE_PROMPT + 证据链 + current + issues）→
        _adopt_refined_draft 先接管完整稿与完成轮数 → 护栏③（归账并接管新稿后复查）→
        命中则采用 refined 降级停机；
        否则回到循环顶部重新自查修正稿
```

> 真迭代依据（工业标准）：Self-Refine 每轮用新 feedback；LangGraph「revise 后必 re-reflect，否则循环无效」。复用同一批 issues 反复修正是反模式（[REASON-009](../../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md)）。

## 对外接口

> 对外接口 = 被 agent/ 层编排依赖的策略类。`ReflectionAgent._strategy_cycle()` 委托 `ReflectionStrategy.execute()`。

| 策略类 | 契约（方法） | 说明 |
| --- | --- | --- |
| `ReflectionStrategy` | `execute(...)` + `outcome` | 完整契约见上文；被 ReflectionAgent 编排 |

`execute` 必填 `run_id` 与 `run_stop`，可选接收 `workflow_id` 和
`parent_cancel_events`。收集阶段的 ReAct 继承同一组运行控制，不新建 run 或重置父级期限；
同一个 ReflectionStrategy 实例不能并发执行。

## 边界情况

| 场景 | 处理 |
| --- | --- |
| 模型未调用 final_answer（stop 自由文本） | 降级：content 保留、structured=None、degraded=True |
| ReAct 失败（LLM 错误等） | error 透传、证据链保留、degraded=True |
| 自查返回 None / 拒答 / 熔断等不可恢复错误（AppError） | CRITIQUE_FAILED 分发 → 默认采用 draft（degraded=True） |
| 修正返回 None / 熔断等不可恢复错误 / 达 max_refine_rounds 上限 | 采用最近稿（degraded=True） |
| 结构化输出截断 | 集成层短路返回 None → 走自查/修正 None 降级路径（本层无感知） |
| 自查/修正抛非 AppError 编程错误（TypeError 等） | 不吞，向上冒泡（fail fast） |
| 循环中用户取消（cancel_event 置位） | 自查前与修正前准入、修正归账后复查；命中即停机降级采用最近完整稿，不再多发下一笔结构化调用；若命中时自查已通过（此后无付费动作），产出干净成功 |
| 总时长超限（elapsed > max_execution_time） | 停机降级采用最近稿（degraded=True，error 标注执行超时） |
| 结构化调用中取消/超时（E） | `_critique`/`_refine` 的 `generate_structured` 透传 `cancel_event` + 绝对 `deadline`；信号约束每笔 reserve/create/retry，并在 extract 最外层收敛 None，外层按既有路径停机降级 |
| critique/refine 结构化输出超预算 | 走 generate_structured 默认预算（settings.llm_structured_max_tokens）；截断由集成层短路返回 None → 走 None 降级（不崩溃，P4） |
| 同一实例并发 / 多次 execute | 不并发复用——outcome/_structured_usage 被覆盖，每次运行新建或串行读取（P4） |
| 证据链含 final_answer 条目（校验失败留痕，非真实证据） | critique 序列化时经 `prompts/_reflection_payload.py` 剔除（以字面量实现——prompts 不依赖 reasoning，规避环） |
| 证据链为空 + draft 引用不存在证据 | 自查 grounding 维度抓出（Grounding 价值） |
| 语义缩减后最小提示骨架仍超过 `max_context_tokens` | 本地零结构化调用，按 CONTEXT_EXCEEDED 保留最近完整稿 |
| 预缩减请求仍被 Integration 最终预算闸拒绝 | 当前阶段只调用一次，不缩减重试；保留原始证据、最近稿和已取得 critique |

## 配置项清单

配置键的完整定义与默认值见 [配置参考](../../config_doc/config.md)；本节仅记录与本组件相关的行为。

| 配置 | 说明 |
| --- | --- |
| `agent_max_refine_rounds` | Reflection 报告生成最大尝试轮数（初稿 1 + 至多 N-1 次修正）；经 `AgentContext.max_refine_rounds` 注入 |
| `agent_max_tool_protocol_retries` | 初稿 ReAct 的工具调用协议修正上限；三类协议错误共享连续预算 |

## 测试状态

`tests/unit/test_reflection.py` + `tests/unit/test_reflection_agent.py`：

- 三阶段全路径：自查 ok 采用初稿 / 自查 issues 修正 + 复查 ok 采用 refined
- 真迭代：修正后复查新 issues 再修正（refine_rounds=2）/ 达上限采用最后修正稿（degraded）
- Grounding 注入断言（自查入参含证据链 + 初稿）
- 降级路径：自查失败 / 拒答 / **不可恢复 AppError（熔断 / LLMAPIError 401/403）** / 修正失败 / ReAct 无 structured / ReAct 失败
- 非 AppError 编程错误不吞、向上冒泡（fail fast）
- done 事件 total_tokens 与 outcome 一致 + 抑制 ReAct 中间 done（P2 / REASON-011，事件流仅收尾 1 个 done）
- 反思循环护栏覆盖调用前准入、无结果终止分类、修正返回后的成果/usage 接管，以及自查通过后的成本/after-turn cancel 与 strict deadline 差异；上下文超限不进入 CRITIQUE_FAILED
- max_refine_rounds 上限（max=1 不修正）、CRITIQUE_FAILED 分发（RAISE/STOP）、Schema 校验
- Scope 盲区清单维度穷举（9 dimension）、护栏透传
- 长 evidence/draft/issues 的字段优先级、省略计数与源对象不变；最小骨架零调用、最终拒绝单调用及 critique 保留
- 桥接：_map_outcome 映射 / ctx 透传 / 端到端 / 默认向后兼容

## 设计决策

- 三阶段显式分离动机、grounding 反内在自查（Huang et al.）、schema 常量+注入取舍、新增 CRITIQUE_FAILED 理由、降级路由、cost 缺口取舍——见 [ADR reflection-strategy](../../../adr/domain/reasoning/2026-08-31-reflection-strategy.md)

## 问题记录

- [REASON-009 修正循环复用同批 issues](../../../issues/domain/reasoning/2026-09-01-reflect-refine-loop.md)：复用同一批 issues 反复修正是反模式——修正后必重新自查（真迭代）
- [REASON-010 自查/修正失败降级缺口](../../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)：不可恢复 AppError / 结构化截断未覆盖降级——统一 CRITIQUE_FAILED 分发 + 集成层短路返回 None
- [REASON-011 done 事件 token 口径](../../../issues/domain/reasoning/2026-09-01-reflect-done-token-caliber.md)：ReAct 中间 done 泄漏 / total_tokens 口径不一致——抑制中间 done，收尾 1 个 done 与 outcome 一致
- [REASON-016 跨策略执行护栏](../../../issues/domain/reasoning/2026-09-10-cross-strategy-guard-priority.md)：自查/修正调用前后统一判定，取消或成本超限后不再多发下一笔请求
- [REASON-020 护栏复查挂载点](../../../issues/domain/reasoning/2026-09-11-reflection-guard-checkpoint.md)：复查落在自查 ok 判定之前——合格报告被标降级、自查结论丢失；复查改挂到修正调用前
- [REASON-022 结构化 Guard 终态](../../../issues/domain/reasoning/2026-09-12-structured-guard-terminal-loss.md)：保留 REASON-020 的成本/after-turn 语义，strict deadline 与上下文超限按专用终态处理
- [REASON-023 Reflection 语义上下文缩减](../../../issues/domain/reasoning/2026-09-13-reflection-semantic-context-reduction.md)：字段级预算与省略标记；缩减视图不覆盖原始证据、最近稿或 critique
- [ADR-003 调用与提交边界](../../../adr/2026-09-12-sdk-call-guard-response-commit.md)

## 相关文档

- [领域推理纯边界 ADR](../../../adr/domain/reasoning/2026-09-16-strategy-pure-boundaries.md)
- [reflection_benchmark.md](reflection_benchmark.md)（工业级对标基准）
- [推理策略模块](reasoning.md)（主文档）
- [ReActStrategy 策略组件](react.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 ReflectionAgent 桥接）
- [领域层说明](../README.md)
