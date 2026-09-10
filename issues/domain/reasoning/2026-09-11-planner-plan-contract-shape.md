# 规划后护栏分支透出未规范化的 plan 形状（REASON-018）

> **状态**：✅ 已修复 ｜ **优先级**：P2 ｜ **来源**：REASON-016 跨策略护栏复审 ｜ **涉及模块**：`app/domain/reasoning/planner.py`

## 问题描述

### 现象

`PlannerStrategy.execute` 在规划调用返回后复查护栏（取消 / 期限 / 成本）。命中时走降级收尾，该分支把 `_plan()` 的返回值直接塞进 `_finalize(plan=plan, ...)`——而 `_plan()` 返回的是 `generate_structured` 产出的 PLAN_SCHEMA 原始结构。

`PLAN_STEP_SCHEMA` 为 `additionalProperties: False`，其步骤只含 `description` 与 `depends_on`、**不含 `id`**；而 `PlannerOutcome.plan` 的契约（代码注释、`planner.md`、`plan_result` 构造）是 `{goal, steps:[{id, description}]}`。

### 影响

- 同一字段在不同路径下有两种形状：正常路径是规范化结构，护栏路径是 LLM 原始结构；
- `app/domain/agent/planner.py` 把 `outcome.plan` 原样放进 `AgentResult.metadata["plan"]`，因此对外的元数据形状随路径漂移；
- 按 `plan["steps"][i]["id"]` 消费的编排 / 证据链报告（Phase C 规划中）会 `KeyError`，而 `depends_on` 这一内部顺序纪律字段会泄漏到对外元数据。

### 根因

该分支位于 `goal = plan["goal"]` / `_normalize_steps(...)` / `plan_result = {...}` 之前——规范化只发生在主路径上，护栏早退路径绕过了它。规范化的职责属于「对外计划形状」，但实现放在了某一条路径里，而不是一处共用的构造点。

## 工业级参照

- LLM 原始输出与领域模型分离是 Plan-and-Execute 类实现的通行做法（LangChain 的 `Plan`/`PlanStep` 是带索引的领域对象，不是模型返回的 JSON 原样）；本仓的 `_normalize_steps` 正是这层归一，缺的是护栏路径也走它。
- 契约对象在出口处单点构造（single construction point）可避免「同一字段多形状」——本仓既有先例是把口径收敛到 `_finalize` 一个出口，本次把计划形状也收敛到一个构造方法。

## 修复方案

新增 `PlannerStrategy._plan_payload(goal, steps)` 静态方法，作为计划对外形状的唯一构造点；护栏分支与 `plan_result` 共用：

1. 护栏分支（`plan` 非 None 时）：`_plan_payload(plan["goal"], self._normalize_steps(plan["steps"], completed=completed))`——复用 normalize 赋 id 并过滤空描述，快照不含 `depends_on`；
2. 主路径：`plan_result = self._plan_payload(goal, pending)`，删除原字面量。

流程未重排：护栏仍优先于「空计划」判定与 ReAct 兜底分支，文案与 `plan is None` 的区分不变。

### 决策取舍

选择就地修正取值而非把 `goal`/`pending`/`plan_result` 整块上移到护栏之前：后者会让「计划为空」的降级文案盖掉取消 / 超时信号，破坏「护栏优先」这一既有不变量。代价是护栏分支多做一次 `_normalize_steps`（纯函数、无副作用、步骤数有 `maxItems` 上限）。

## 实施记录

| 范围 | 实施内容 |
| --- | --- |
| `planner.py` | 新增 `_plan_payload`；护栏分支改用契约快照；`plan_result` 复用同一构造点 |
| 测试 | 新增 `test_guard_after_plan_returns_contract_plan_snapshot`（置位取消于 plan 调用返回后，断言快照含 id、无 `depends_on`、`react_calls == 0`） |
| 文档 | `planner.md` 边界情况表补「规划后护栏命中」行 |

## 验证

- `tests/unit/test_planner.py::test_guard_after_plan_returns_contract_plan_snapshot`：修复前断言失败（实测 `steps` 为 `[{description, depends_on}]`），修复后通过。
- `tests/unit/test_planner.py` + `tests/unit/test_planner_agent.py` 全通过；全量测试与 alignment 结果记录在 `docs/todo.md`。

## 教训沉淀

契约字段的形状必须在出口处单点构造。把归一化写在「主路径」上，等于给每条早退路径留了一份未归一的副本——早退路径平时不执行，形状漂移不会被现有测试发现，直到下游按新字段消费时才炸。
