# Reflection done 事件 total_tokens 口径不一致（REASON-011）

> **模块**：`app/domain/reasoning/reflection.py`（ReflectionStrategy 收尾 `_finalize`）
> **发现日期**：2026-09-01 ｜ **状态**：✅ 已修复 ｜ **编号**：REASON-011

## 发现

工业级完成度评审发现：`_finalize` 的 `build_done_event` 只用 `react_outcome.total_tokens`（收集阶段），而 `outcome.total_tokens` 含自查/修正全阶段累计（`+ self._structured_usage`）——**SSE done 事件与结果对象两个事实源漂移**。前端/监控按 done 事件统计成本时，critique/refine 的 token 用量全部漏计，成本审计失真。

## 分析

**根因**：done 事件字段直接复用 `react_outcome.total_tokens`，未叠加 `_structured_usage`（自查/修正累计）。`_finalize` 是统一收尾点，事件字段与 outcome 字段本应共享同一累计值——两个字段各写一次导致口径分叉。

## 修复

`_finalize` 的 `build_done_event` 的 `total_tokens` 改为 `react_outcome.total_tokens + self._structured_usage.get("total_tokens", 0)`（与 `outcome.total_tokens` 一致）。

## 验证

`tests/unit/test_reflection.py` 新增 `test_reflect_done_event_tokens_match_outcome`：done 事件（收尾的最后一个）`total_tokens` 与 `outcome.total_tokens` 一致（修复前 0 vs 90 失败，复现）。test_reflection.py（25 用例）+ test_reflection_agent.py（5 用例）通过，全量 pytest 无回归。

## 教训

1. **SSE 事件与结果对象同源统计须同一口径**：done 事件是监控事实源，漏计成本会失真；收尾统一组装点（`_finalize`）里事件字段与 outcome 字段应共享同一累计值，不各自拼一次。
2. **复用子组件的 done 事件会混淆事实源**：Reflection 复用 ReAct 收集阶段的 done（total_tokens 只含收集），再加自己的 done——事件流里两个 done 口径不同。**已实施抑制**：Reflection 透传 ReAct 事件时过滤中间 done（`AgentEventType.DONE.value` 匹配），事件流仅保留收尾的 1 个 done（`test_reflect_suppresses_react_done_event` 锚定，降低事件流噪音）。
