# Reflection 护栏复查挂载在自查 ok 判定之前，合格报告被改判降级（REASON-020）

> 2026-09-12 后继：[ADR-003](../../../adr/2026-09-12-sdk-call-guard-response-commit.md)
> 保留本文关于成本和 after-turn 取消的结论；strict deadline 迟到成功及无结果 Guard
> 分类由 [REASON-022](2026-09-12-structured-guard-terminal-loss.md) 部分替代。
> **状态**：✅ 已修复 ｜ **优先级**：P2 ｜ **来源**：REASON-016 跨策略护栏复审 ｜ **涉及模块**：`app/domain/reasoning/reflection.py`

## 问题描述

### 现象

Reflection 的护栏复查挂在「自查调用返回 + usage 归账」之后，而该位置在 `critique.get("ok")` 判定**之前**。当自查返回 `ok=True`（报告已合格）但归账后累计成本恰好越过阈值时，护栏先命中，运行以 `_finalize_guard` 收尾：

``` python
success = bool(current)      # True（稿子可用）
degraded = True              # 尽管自查已通过
error = "成本超限（累计 $X），采用最近稿（降级）"
critique = None              # 自查的 ok 结论被丢弃
```

即同一 outcome 内 `success=True` 与 `degraded=True` 同时成立，且自查结论丢失。

### 影响

- 下游按 `degraded` 判断报告可信度时，一份**已通过自查**的完整根因报告被标为降级产物；
- `outcome.critique` 恒为 None，自查结论（维度 / issues）在结果对象中不可得；
- 自查之后不再有任何付费调用，因此这次复查没有任何被门控的对象——它唯一的效果就是改判与丢字段。

### 根因

护栏的挂载规则是「付费调用前准入 + 调用成功并归账后复查」，但「归账后是否还有付费调用」取决于分支：自查之后可能走 `ok` 早退、达上限早退（都不再付费），也可能走 `refine`（付费）。复查被放在分支判定之前，等于对两条「不再付费」的路径也做了准入检查，而准入检查在无后续调用时没有意义。

## 工业级参照

- 预算门控（admission control）应当恰好包住**下一笔**将要发起的调用：有调用才准入，无调用不判定。本仓 ReAct 的护栏就遵循这一点——每次判定都紧邻一次 LLM 调用。
- 与 Planner 的 `_finalize_guarded_summary` 对比：那里护栏命中意味着运行**终止**（必须给出终止原因），标 `degraded=True` 有语义；而本处的命中不改变任何后续行为，仅改判已有结果，两者不可类比。

## 修复方案

把该复查整块移到「`critique.ok` 早退」与「达修正上限早退」之后、`_refine(...)` 调用之前，使其成为纯粹的**修正调用前准入**。

改动后的三个挂载点：

| 挂载点 | 位置 | 作用 |
| --- | --- | --- |
| 护栏① | 循环顶部、自查调用前 | 门控自查这笔付费调用 |
| 护栏② | 两条早退之后、`_refine` 之前 | 门控修正这笔付费调用 |
| 护栏③ | 修正返回、归账并接管新稿之后 | 命中则保留已付费的修正稿再终止 |

`refine_round` 参数无需调整（新位置仍在 `refine_round += 1` 之前）；`crit_usage` 归账保持原位，故护栏②看到的累计用量仍含本次自查开销。

### 决策取舍

- **达上限 + 超限时以「达到修正上限」文案收尾**：两条路径都 `degraded=True` 且不再付费，差异仅在 error 文案与 `critique` 是否保留；保留自查结论的收益更大。
- **自查在途发生的取消，若自查已通过则不再体现在 outcome 上**：产出已就绪且无后续付费，报成功更符合用户预期（`/chat/stop` 的目标是止损，不是丢弃已完成结果）。取消信号在其他路径（自查未通过、修正在途、循环顶部）仍照常终止。
- 未改动护栏优先级契约与 `CostLimiterPort`，仅移动挂载点。

## 实施记录

| 范围 | 实施内容 |
| --- | --- |
| `reflection.py` | 护栏复查块从「归账后」移到「两条早退之后、`_refine` 之前」，并补注释说明挂载理由 |
| 测试 | 新增 `test_reflect_critique_ok_after_cost_limit_is_clean_success`（决策回归锁，修复前必失败）与 `test_reflect_refined_adopted_on_cost_limit_after_refine`（补齐既有覆盖声明） |
| 文档 | `reflection.md` 执行流程 / 边界情况表 / 测试状态 / 问题记录；`_common.md` 收窄「三个策略都在归账后复查」的表述 |

## 验证

- 新增的决策回归锁用例修复前实测失败：`critique=None`、`error='成本超限（累计 $30.0），采用最近稿（降级）'`；修复后 `degraded=False`、`error=None`、`critique` 保留。
- 既有两条护栏用例（`test_reflect_cost_limit_stops` / `test_reflect_cancel_event_stops_degrades_to_draft`）的脚本首项均为 `{"ok": False, ...}`，修复后仍由护栏②命中，断言全部保持。
- `tests/unit/test_reflection.py` + `tests/unit/test_reflection_agent.py` 全通过；全量测试与 alignment 结果记录在 `docs/todo.md`。

## 教训沉淀

「调用前准入 + 归账后复查」这条规则里的「后」，指的是**下一笔调用之前**，不是「上一次调用返回之后」。把复查放在分支判定之前，会让它为一条不再付费的路径做准入——此时它不阻止任何开销，只改判已有结果。挂载点必须紧邻它要门控的那笔调用。
