# 关闭期连接拦截缺少直接断言

> **ID**：DB-008 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P3
> **发现来源**：DB-F02 后续发现登记（2026-09-23）；尚未接入应用或发布。
> **范围**：`_connect` 的关闭期拦截与其测试覆盖。

## 现象与证据

组件文档的行为边界登记了“关闭期新建连接 → 终止该驱动并抛 `DatabaseRuntimeError("closing")`”，但没有任何用例断言它。现有 `test_dispose_preserves_external_connection_during_initialization` 走的是相反分支：状态为 `ready` 时连接被保留并计入 Owner，使 `dispose()` 报 `close_incomplete`——两者不是同一时序，不能用它代替。

新增 `test_connect_during_close_is_terminated_and_rejected`，按 `closing` / `closed` 两个状态断言驱动被 `terminate()` 且抛出 `closing`。

## 根因与取舍

缺口来自实现顺序：真实引擎用例先覆盖了“连接失败不开放准入”，关闭期拦截只有文档描述、没有断言。补测直接调用池事件处理器（fake 边界），确定性且不依赖外部服务。

该用例是对既有行为的补测，没有天然的“修复前失败”；按“移除修复确认用例会红”的做法，临时移除拦截分支后两条参数化用例都失败，判别力已确认。

**未覆盖**：连接池在关闭期确实会调用 `connect` 这一真实时序，需要可用 PostgreSQL 才能观察（属 DB-F06 的真实环境验收），本次不追求。

## 实施与验证

`tests/unit/test_database.py` 新增参数化用例；`database.py` 未改动。

- 定向：`uv run pytest tests/unit/test_database.py tests/unit/test_database_lifecycle_review.py -q` → 55 passed（本项新增 2 条参数化用例）。
- 全量：`uv run pytest -q` → 1637 passed, 5 xfailed。
- `uv run ruff check .`、`uv run ruff format --check .` 与 `uv run python -m scripts.verify_alignment` 通过。

## 边界与关联

同一轮在 fake 边界同时补了脱敏目标用例（[DB-009](2026-09-23-pool-logger-name-hardcode.md)）。行为正文维护在[组件文档的行为边界](../../../docs/infrastructure_doc/database_doc/database.md#行为边界)，本记录不复写该表。
