# Reflection Payload 组件设计说明

> **对应代码**：`app/domain/prompts/_reflection_payload.py`
> **文档定位**：Prompts 包内部的 Reflection 语义投影协作契约
> **更新日期**：2026-09-13
> **状态与映射**：[ALIGNMENT](../../ALIGNMENT.md)

## 设计目标与边界

该组件为 Reflection 自查和修订请求生成只读的阶段性载荷。一次良率 RCA 可能积累大量
工具结果、长报告和多轮问题清单；组件在有限 token 容量内优先保留可验证结论所需的
证据、稿件骨架和待修问题，避免模型把被省略内容误认为不存在。

它只由同包 `PromptManager` 调用，不是模块公开 API。组件不修改原始 evidence、draft
或 issues，不调用模型，不决定 Guard 终态，也不负责 provider 最终窗口准入。业务对象
定义见[产品文档](../../project/product.md#良率-rca-的业务对象与报告生命周期)。

## 内部协作契约

| 入口 | 输入 | 输出 | `PromptManager` 的责任 |
| --- | --- | --- | --- |
| `serialize_critique_sections` | `evidence`、`draft`、`available_tokens`、`count_tokens` | `(evidence_text, draft_text)` | 先扣除 `CRITIQUE_PROMPT` 固定开销，再把两个字符串填回模板 |
| `serialize_refine_sections` | `evidence`、`draft`、`issues`、`available_tokens`、`count_tokens` | `(evidence_text, draft_text, issues_text)` | 先扣除 `REFINE_PROMPT` 固定开销，再填回模板 |

`available_tokens=None` 或 `count_tokens=None` 表示兼容路径。兼容路径仍会排除
`final_answer` 证据记录，并以既有 4000 字符上限形成 evidence 文本；draft 和 issues
使用完整 JSON 序列化。预算路径用调用方注入的 token 计数器判断候选视图是否可容纳。

组件返回字符串而非新的业务对象。省略标记位于 JSON 文本之外，不能被调用方反序列化
后写回 draft 或 issues。

## 核心业务优先级

### Evidence

每条可用证据渲染为稳定来源行：

```text
[E0001] tool=<工具名> params=<查询参数> success=<状态> error_code=<错误码> result=<结果> anchors=<量测/时间锚点>
```

- `tool == "final_answer"` 是报告出口，不作为独立工具证据；其原始位置仍参与编号，
  因此保留下来的 `E####` 可以出现间隔。
- 当前稿 `supporting_evidence` 已引用的记录优先，保证已有结论仍可被复核。
- 其余记录按最近取得优先选择；最终输出恢复原始证据顺序，避免时间关系被重排。
- 同一记录依次尝试完整、缩短参数和结果、仅保留身份与状态，最后才整条省略。
- 长结果中的日期、时间和带单位量测值会额外提升为 `anchors`，便于跨来源关联。

引用匹配使用工具名和查询参数值作保守识别。它服务于上下文保留优先级，不替代最终
报告对证据引用真实性的校验。

### Draft

完整 JSON 能容纳时保持原稿。需要缩减时先投影：

- `summary`
- `conclusions`，包括 claim、supporting_evidence 与 confidence 等现有字段
- `explicit_abstention`

随后逐级降低字符串长度和列表项数量。`next_steps`、`assumptions` 等字段在紧张预算下
可先退出送审视图，但仍保留在调用方持有的原始稿件中。

### Issues

修订阶段先保留 `severity == "critical"` 的问题；其他问题按较新记录优先。每个候选
仍按统一层级缩短字符串和列表。该排序只影响本轮修订可见视图，不重排原始问题清单。

## 预算分配与缩减流程

Critique 的动态容量初始按 evidence 60%、draft 40% 分配；Refine 按 evidence 45%、
draft 35%、issues 20% 分配。每个分区先取自身实际需求与初始份额的较小值，未使用容量
再按 evidence、draft、issues 的顺序回流给仍未装下的分区。

```text
完整候选表示计量
    │
    ├─ 需求总量可容纳 → 返回完整分区
    │
    └─ 超限 → 按阶段权重初分配 → 回流余量
              ├─ evidence：引用优先 + 最近优先 + 单条降采样
              ├─ draft：报告骨架投影 + 递归降采样
              └─ issues：critical 优先 + 最近 minor 优先 + 递归降采样
```

需求计量必须使用与最终候选载荷相同的表示。证据需求通过完整 `_evidence_line` 集合
计算，不能用原始 JSON 的近似长度代替，否则会在总预算充足时错误省略记录。

## 省略与最小骨架

省略统一使用可审计标记，例如：

```text
<omitted section="evidence" records="3" reason="context_budget"/>
<omitted section="draft" items="4" characters="820" reason="context_budget"/>
```

证据标记统计整条省略数量；draft/issues 标记统计列表项和字符串字符差额。标记本身也
参与 token 计量。若分区容量小到连标记或空 JSON 加标记都不能容纳，组件仍返回可识别
的最小视图；`PromptManager` 不吞掉它，`ReflectionStrategy` 对完整 prompt 复查后形成
零 SDK 调用的上下文 Guard。

## 行为边界

| 场景 | 行为 | 不变量 |
| --- | --- | --- |
| 预算充足 | 返回完整候选表示，不附省略标记 | 原始对象不变 |
| evidence 中包含 `final_answer` | 不进入证据行 | 报告出口不伪装成来源证据 |
| 引用证据与最近证据竞争 | 先选择已引用记录，再选择最近记录 | 输出仍按原始索引排序 |
| draft 或 issue 含深层列表 | 递归限制字符串与列表项 | 省略标记位于 JSON 外 |
| 极小或负动态预算 | 返回最小可识别视图，可能仍超预算 | 不删除必须的审查语义来伪造“可发送” |
| 输入或计数器异常 | 异常直接传播 | 组件不把编程错误转换成 Reflection 降级 |

该组件没有异步任务、资源所有权、usage 或取消处理。

## 验证入口

[tests/unit/test_prompts.py](../../../tests/unit/test_prompts.py) 覆盖：

- 预算充足时 evidence 和 draft 与真实序列化形状一致；
- `final_answer` 排除与稳定 `E####` 编号；
- 被引用证据、最近证据、量测和时间锚点的保留；
- critical/较新 issue 的优先级；
- 输入只读、省略计数和极小预算视图；
- 证据需求使用最终候选表示，避免充足预算误省略。

当前实现、文档和测试映射以 [ALIGNMENT](../../ALIGNMENT.md) 为准，不在本文维护通过数量。

## 设计决策与问题记录

- [语义上下文预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)：计量能力与领域字段选择的分层决定。
- [REASON-023](../../../issues/domain/reasoning/2026-09-13-reflection-semantic-context-reduction.md)：Reflection 阶段性载荷缩减闭环。
- [REASON-024](../../../issues/domain/reasoning/2026-09-13-reflection-evidence-budget-underestimate.md)：完整证据表示的需求计量修复。

## 相关文档

- [Prompts 模块公开契约](prompts.md)
- [ReflectionStrategy](../reasoning_doc/reflection.md)
- [Planner payload 组件](planner_payload.md)
- [ContextBudgetPort](../ports_doc/ports.md)
