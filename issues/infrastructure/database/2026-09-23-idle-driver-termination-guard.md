# 空闲驱动的强制终止缺少 Owner 过滤

> **ID**：DB-006 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P3
> **发现来源**：关闭准入流程解析核对（2026-09-23）；尚未接入应用或发布。
> **范围**：`dispose()` 兜底路径的强制终止 `_terminate_idle_drivers`。

## 现象与证据

`_terminate_idle_drivers` 遍历全部已登记驱动并强制 `terminate()`，不判断 Owner，安全性完全依赖调用方纪律（原注释：调用者已确认没有 borrowed/probe owner）。当前唯一调用点在 `dispose()` 的兜底分支，那里的守卫已保证“仍打开的驱动必然 Owner 为 `None`”，**因此没有现存缺陷**；但函数名承诺的是“空闲驱动”，实现却对任何状态都执行，后来者从别的路径调用会直接强关在用事务。

红测 `tests/unit/test_database.py::test_idle_termination_never_touches_an_owned_driver`：connect 事件登记当前任务为 Owner 后调用该函数，修复前驱动被 terminate（`terminations == 1`），修复后不动，归还（checkin）后才终止。

## 根因与取舍

“不能强关在用事务”这条不变量被放在调用方注释里，而不是放在执行终止的地方；同为终止入口的 `_terminate_probe_drivers` 已按 `owner is self._probe_task` 过滤，两者约束强度不一致。

采用方案：在该函数内加 `owner is None` 过滤，使“只处理空闲驱动”成为代码约束而非约定，与探针版对称。放弃了另外两个方向：只加断言——断言在 `-O` 下消失，且只报告违规不阻止强关；依赖调用方注释——正是本次要消除的依赖。

代价说明：按项目“不为不可达场景加防线”的倾向，本轮本可只补文档。判断依据是该函数名与实现不一致本身就构成维护缺陷，就地收紧比长期维护一条调用方纪律成本更低。

## 实施与验证

`app/infrastructure/database.py` 的 `_terminate_idle_drivers` 改为遍历 `self._drivers.items()` 并要求 `owner is None and not driver.is_closed()`；原“调用者已确认”注释改写为函数 docstring，写明限定范围与理由。

- 定向：`uv run pytest tests/unit/test_database.py tests/unit/test_database_lifecycle_review.py -q` → 51 passed（新增 1 条）。
- 全量：`uv run pytest -q` → 1633 passed, 5 xfailed。
- `uv run ruff check .`、`uv run ruff format --check .` 与 `uv run python -m scripts.verify_alignment` 通过。

## 边界与关联

该过滤不改变现有可达行为：`dispose()` 的守卫已保证兜底分支里仍打开的驱动 Owner 为 `None`，改动只是让这个前提由代码承载。业务借出的连接仍由使用方排空，`dispose()` 在仍有 Owner 时保留资源并报 `close_incomplete`（[DB-003](2026-09-22-runtime-resource-ownership.md)）。约束正文维护在[组件文档的关键实现说明](../../../docs/infrastructure_doc/database_doc/database.md#关键实现说明)，本记录不复写资源责任表。
