# Planner 步骤成功判据多出一维，异常收尾的有产出步骤被误判失败（REASON-019）

> **状态**：✅ 已修复 ｜ **优先级**：P1 ｜ **来源**：REASON-016 跨策略护栏复审 ｜ **涉及模块**：`app/domain/reasoning/planner.py`

## 问题描述

### 现象

Planner 的步骤成功判据实现为三个维度：

```python
ok = sub.success and not sub.error and bool(sub.content.strip())
```

而紧邻注释（`步骤判成功 = react success 且产出非空`）、`planner.md`（设计目标 / Replan 触发 / 执行流程 / 边界情况四处）与 planner ADR（Decision 3 / Consequences）**全都只声明两个维度**：react 成功 且 产出非空。

多出的 `not sub.error` 改变了「有产出但异常收尾」这一步的判定。ReAct 侧有三个终态会同时给出 `success=True` 与非空 `error`：`_finalize_max_turns`（达到迭代上限但有产出）、`_finalize_guard_result`（取消 / 超时 / 成本 / 上下文超限但有产出）、`_finalize_unknown`（未捕获异常但保留了可见成果）。其中护栏类终态由 planner 自己的 `_current_guard()` 先行接管，不会落到本判据；**实际受影响的是 `MAX_TURNS` 与 `UNKNOWN`**。

### 影响

- **成本曲线变化（未记录）**：达到每步迭代上限但已产出内容的子跑——这是长步骤的常态——从「成功」改为「失败」，进而触发 replan：至多 `max_replan_rounds` 次付费 `generate_structured(REPLAN_SCHEMA)` 调用、随后的付费 `summarize`、以及替换步骤的重跑。
- **审计与报告口径失真**：`steps_executed[i]["success"]` 翻为 False、`error` 填入「已达到最大迭代次数(N)」，经 `_step_messages` / `_plain_summary` 渲染成「失败(...)」，并影响 `any(r["success"] ...)` 驱动的部分汇总判定。
- **与文档口径不符**：四处设计文档都写着二维判据，改判行为没有任何文档或测试锚定。

### 根因

REASON-016 统一护栏调用后复查顺序时，该行被顺带加上了 `not sub.error`，动机是「防止被护栏终止的子跑记为成功」。但护栏终止的子跑本就被紧随其后的 `_current_guard()` 拦下（该分支不会走到判据之外），因此这一维没有解决它想解决的问题，反而把 ReAct 的 `success` 语义重新解释为「干净完成」——而 ReAct 的契约一直是「`success` = 有可用产出」（`_finalize_max_turns` 正是这样设定的）。

## 工业级参照

- 停止原因与产出可用性分离是通行做法：OpenAI 兼容协议的 `finish_reason=length`（截断）仍携带可用内容，各 SDK 都不会因为「非正常结束」丢弃该内容；本仓 ReAct 的 `success` / `error` 分工与之同构——`success` 管可用性、`error` 管原因。
- 恢复预算的触发条件应当是「无可交付产出」，而不是「结束得不干净」：LangGraph 的节点失败判定同样基于产出而非中断原因。用中断原因触发恢复，会把局部超时放大成全量重跑（付费）。

## 修复方案

判据回到文档已声明的二维：

```python
ok = sub.success and bool(sub.content.strip())
```

并在注释中写明 `sub.error` 不参与判据的理由与护栏接管关系，防止再次被加回。

### 决策取舍

- 接受「被取消但已有产出的子跑，审计记录 `success=True` 且 `error=None`」：该步确有可用产出；运行级 `outcome.error` 仍是「用户取消，采用已完成步骤（部分进度）」，取消信号不丢。
- 不采用「按子跑终态 kind 判定」的收窄方案：`ReActOutcome` 目前不携带终态类型，需要新增字段并同步 ReAct 契约、文档与 ALIGNMENT，改动面与收益不匹配；且与文档既有的二维口径相悖。

## 实施记录

| 范围 | 实施内容 |
| --- | --- |
| `planner.py` | 判据移除 `not sub.error`；注释补二维口径与 `_current_guard` 接管关系 |
| 测试 | 更新 `test_cancel_after_plan_stops_partial` 的断言（`success is False` → `is True`），docstring 写明二维判据下「有产出即成功」 |
| 文档 | 设计文档本就为二维，零改动；`planner.md` 边界情况表补「子跑达迭代上限但有产出」行 |

## 验证

- 修复前 `test_cancel_after_plan_stops_partial` 断言 `success is True` 即失败（实测该步 `success=True, error='Agent 已被取消'`），修复后通过。
- `tests/unit/test_planner.py` + `tests/unit/test_planner_agent.py` 全通过；全量测试与 alignment 结果记录在 `docs/todo.md`。

## 教训沉淀

「顺手加一个条件」如果改变了已有字段的语义，就必须同时更新声明该语义的文档与测试。本判据的注释、模块文档与 ADR 三处都写着二维，代码却在一次不相关的重构里变成了三维——没有测试锚定的语义变更不会红，只会在账单上体现。
