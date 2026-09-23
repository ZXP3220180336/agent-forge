# 运行期自身原因码被归类为未知缺陷

> **ID**：DB-005 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P3
> **发现来源**：DB-F02 提交前审查（2026-09-22）第 2 项；尚未接入应用或发布。
> **范围**：`_reason` 的原因码分类与探针的准入拒绝路径。

## 现象与证据

探针在 `_probe_stopped` 置位后仍继续 checkout 时，`_checkout` 复查准入抛 `DatabaseRuntimeError("close_incomplete")`。`_reason` 的受信集合只含检查器可用的三个码，于是它被判为未知程序缺陷，`probe()` / `init()` 抛 `DatabaseRuntimeError("internal_error")` 而不是返回带准确原因码的状态；`_connect` 在关闭期抛出的 `closing` 同理。终态仍 fail-closed，准入结果不变，但原因码失去诊断价值。

红测两条：`tests/unit/test_database.py` 的参数化 `_reason` 分类用例，以及 `test_stopped_probe_keeps_close_incomplete_instead_of_internal_defect`（fake 连接在 `start` 时置 `_probe_stopped` 复现探针吞取消后继续建连）。修复前两者分别返回 `internal_error`、抛 `DatabaseRuntimeError`；另有 `test_untrusted_runtime_error_text_still_becomes_internal_defect` 守住集合外文本不回显。

## 根因与取舍

`_reason` 的受信集合按“检查器可用码”划定，遗漏了运行期自身也会抛出的稳定码。这与已接受契约不符，而非契约需要变更：[DB-ADR-001 D5](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d5公共-api-与能力契约) 的 reason 白名单本就含 `closing` 与 `close_incomplete`，[组件文档](../../../docs/infrastructure_doc/database_doc/database.md#原因码)的原因码表也把它们列为合法原因码。

采用方案：把受信集合提为模块常量 `_TRUSTED_REASON_CODES` 并补入这两个码，仍按码值匹配；集合外文本归未知缺陷，避免回显任意异常文本。不按异常来源区分——那需要新增异常子类才能成立（E9 无证据支持的抽象），且 `_require_ready` 抛出的是状态原因码本身，来源区分收益有限。

## 实施与验证

`app/infrastructure/database.py` 新增 `_TRUSTED_REASON_CODES`，`_reason` 改为 `code in _TRUSTED_REASON_CODES` 判定：受信检查器的三个码与运行期两个码统一在一处维护。

- 定向：`uv run pytest tests/unit/test_database.py tests/unit/test_database_lifecycle_review.py -q` → 50 passed（新增 4 条：参数化 2 条、未受信文本 1 条、集成 1 条）。
- 全量：`uv run pytest -q` → 1632 passed, 5 xfailed。
- `uv run ruff check .`、`uv run ruff format --check .` 与 `uv run python -m scripts.verify_alignment` 通过。

## 边界与关联

只影响分类，不放开准入：`closing` / `close_incomplete` 仍按 fail-closed 处理，只是以原码上报。另一条 P3（`ping()` 在内部缺陷时写入准入状态）已在 [DB-004](2026-09-23-ping-admission-write.md) 闭合。原因码正文维护在[组件文档](../../../docs/infrastructure_doc/database_doc/database.md#原因码)与 DB-ADR-001 D5，本记录不复写完整映射表。
