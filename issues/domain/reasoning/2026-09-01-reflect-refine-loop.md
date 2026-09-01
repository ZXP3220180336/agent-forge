# Reflection 修正循环缺陷：复用同一批 issues 反复修正（REASON-009）

> **模块**：`app/domain/reasoning/reflection.py`（ReflectionStrategy 阶段三）
> **发现日期**：2026-09-01 ｜ **状态**：✅ 已修复 ｜ **编号**：REASON-009

## 发现

审查 Reflection 修正循环时发现：`for refine_round in range(1, max_refine_rounds)` 每轮都传**同一批 `issues`**（第一轮自查针对初稿发现的），修正成功后 `best = refined` 继续下一轮——下一轮用旧 issues 再修正已经修正过的稿。这是**重复应用同一修正**，不是迭代（迭代需要新反馈），无意义甚至有害（可能把改好的改回去）。

**默认配置掩盖**：`max_refine_rounds=2` 时 `range(1, 2)` 只循环 1 次，碰巧是「单次修正」。配置为 3+ 时暴露重复修正缺陷。

## 分析

1. **根因**：迭代结构（for 循环）与「refine 后不二次自查（re_critique=False）」决策矛盾——若不复查，修正 1 次即应采用、立即结束，`for` 循环 + `max_refine_rounds > 2` 无意义。
2. **工业调研确认**（2026-09-01）：LangGraph Reflection 明确「每次 revise 后必须重新 reflect 新稿，**否则循环无效**」（generator must reference the prior critique, or the loop has no effect）；Self-Refine 每轮用新 feedback（针对当前输出 y_t），收益递减（Δy0→y1 > Δy1→y2）。**复用旧 issues 反复修正是反模式**。
3. **工业收敛前提**：迭代 = 修正 → 重新自查 → ok 停 / 新 issues 再修正；cap 2-3 轮 + early exit（自查通过即停）。

## 修复

重构 `execute` 阶段二+三为**真迭代**（while 循环）：

```
current = draft（初稿）
while True:
    critique = 自查(current)          # 每次循环重新自查当前稿
    if 自查失败 → 降级采用 current（degraded）
    if critique.ok → 采用 current（degraded=False）     # early exit
    if refine_round >= max_refine_rounds - 1 → 达上限，采用 current（degraded）  # cap
    issues = critique.issues
    refine_round += 1
    refined = 修正(current, issues)
    if 修正失败 → 降级采用 current（degraded）
    current = refined                 # 回到循环顶部 → 重新自查修正稿
```

- **修正成功后重新自查** refined，ok 采用 / 新 issues 再修正（真迭代关键：新反馈驱动下一轮）
- **达上限判断** `refine_round >= max_refine_rounds - 1`（max_refine_rounds = 报告生成尝试总次数 = 初稿 + 至多 N-1 次修正；max=1 时初稿有 issues 即不修正）
- 错误分发封装为 `_critique` / `_refine` / `_dispatch_critique_failed_result`（CRITIQUE_FAILED：RAISE 抛 / STOP 降级 success=False / CONTINUE 降级 best）

## 验证

`tests/unit/test_reflection.py`：

- 新增 `test_reflect_recritique_issues_refines_again`：修正后复查发现新 issues → 再修正 → 复查 ok → 采用（refine_rounds=2）
- 新增 `test_reflect_reaches_limit_adopts_last`：复查仍有 issues 且达上限 → 采用最后修正稿（degraded=True）
- 更新 `test_reflect_issues_refine_adopts_refined`：补复查脚本（自查 issues → 修正 → 复查 ok → 采用）
- `test_reflect_max_refine_rounds_one_no_refine`：max=1 时初稿有 issues 即不修正（达上限判断修复）
- 全量 22 passed（test_reflection 17 + test_reflection_agent 5）

## 教训

1. **循环迭代必须有新反馈驱动**：迭代 = 修正 → 重新自查，复用旧反馈的「循环」不是迭代而是重复。
2. **cap + early exit 是收敛前提**：max_refine_rounds 兜底防死循环，自查通过（early exit）即停省钱——工业「多数任务 2 轮内解决」。
3. **max_refine_rounds 语义要明确**：= 报告生成尝试总次数（初稿 + 至多 N-1 次修正），非「修正轮数上限」——达上限判断须 `>= N-1`，否则 max=1 也会误入修正。
