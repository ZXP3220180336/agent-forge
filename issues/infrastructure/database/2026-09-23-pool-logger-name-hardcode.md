# 池实例 logger 名硬编码，换池实现会静默失效

> **ID**：DB-009 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P3
> **发现来源**：DB-F02 后续发现登记（2026-09-23）；尚未接入应用或发布。
> **范围**：`_build_engine` 的脱敏过滤器目标。

## 现象与证据

过滤器原本按 `sqlalchemy.pool.impl.AsyncAdaptedQueuePool.<name>` 拼名字挂载。`_engine_options` 目前不含 `poolclass`，asyncpg 方言的默认池正是这一类，所以当前成立；一旦换池实现，过滤器会挂到一个永不发声的 logger 上：池事件日志不再脱敏，且不会有任何测试失败（`echo` 仍由 engine 实例 logger 覆盖）。

新增 `test_log_redaction_follows_the_engine_instance`：测试装置的池 logger 名刻意不用 `AsyncAdaptedQueuePool`。修复前池那条断言失败（漏挂），修复后通过。

## 根因与取舍

把“目标 logger 名”写成了对 SQLAlchemy 池命名布局的假设，而不是从实例读取事实。修法：从实例的 engine logger 与池 logger 取到真正接收记录的 `logging.Logger` 再挂过滤器。

过程中确认一处实现细节：SQLAlchemy 的实例 logger 是 `Logger | InstanceLogger` 联合——`echo` 开启时给 `InstanceLogger` 包装器，关闭时给 `logging.Logger`；而 `InstanceLogger` 只持有内层 logger（`__slots__ = ("echo", "logger")`），自身没有 `addFilter`。因此按 `isinstance(target, logging.Logger)` 分派，包装器情况下取 `target.logger`，两条路径都落到真正接收记录的 logger 上。

## 实施与验证

`app/infrastructure/database.py` 的 `_build_engine` 改为遍历 `(engine.sync_engine.logger, engine.sync_engine.pool.logger)`，包装器情况下取内层 `logging.Logger`。

两条分支各有覆盖：测试装置提供普通 `logging.Logger`，真实引擎 `echo=True` 的脱敏用例提供包装器分支。

- 定向：`uv run pytest tests/unit/test_database.py tests/unit/test_database_lifecycle_review.py -q` → 55 passed（本项新增 1 条用例）。
- 全量：`uv run pytest -q` → 1637 passed, 5 xfailed。
- `uv run ruff check .`、`uv run ruff format --check .` 与 `uv run python -m scripts.verify_alignment` 通过。

## 边界与关联

脱敏强度不变：过滤器仍只挂本实例的 logger，不影响其他引擎；`hide_parameters` 与实例过滤器仍是两层独立保证。同级缺口的补测见 [DB-008](2026-09-23-connect-during-close-assertion.md)，脱敏契约正文在[配置参考](../../../docs/config_doc/config.md)与[组件文档](../../../docs/infrastructure_doc/database_doc/database.md#结构与协作)。
