# ping 观测写入准入状态

> **ID**：DB-004 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P3
> **发现来源**：DB-F02 提交前审查（2026-09-22）第 3 项；尚未接入应用或发布。
> **范围**：DatabaseRuntime 的非完整探测路径（`ping()`）。

## 现象与证据

`ping()` 命中内部缺陷时抛 `DatabaseRuntimeError("internal_error")`，同时把状态由 `ready` 改写成 `unavailable`。这与 DB-ADR-001 D4「`ping()` 只检查真实连接和最小事务，不改变能力准入状态」及 [DB-003](2026-09-22-runtime-resource-ownership.md) 的 ping 只观测语义冲突：一次内部缺陷把已就绪的运行实例降级，观测接口还抛出契约外的异常类型。

红测 `tests/unit/test_database.py::test_ping_internal_defect_neither_raises_nor_changes_admission` 用 fake 连接在 `execute` 抛 `RuntimeError` 造出 internal_error。修复前该用例以 `DatabaseRuntimeError: internal_error` 失败，修复后 `ping()` 返回 `False` 且状态保持 `ready`。

## 根因与取舍

`_run_probe` 的 internal_error 分支缺少 `full` 门控，而同一函数的取消分支已写成 `if full and ...` 才写状态：只有完整探测改写准入是既有意图，该分支漏了同一判断，于是观测路径继承了准入路径的写回。

观测与准入是两类语义不同的检查，失败后果也不同：观测只报告本次结果，准入才决定业务能否取到 Session。工业级划分参见 [Kubernetes 存活与就绪探针](https://v1-34.docs.kubernetes.io/zh-cn/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)——存活观测与是否接流量是两组独立探针，失败分别触发重启与摘除流量，不共用同一状态位。

采用方案：给该分支加 `full` 门控。非完整探测只把原因码回给 `ping()`，由 `ping()` 归约为 `False`；不改 `ping()` 签名，也不新建观测专用通道。放弃的另一方向是把文档措辞改成「内部缺陷会关闭准入」，那等于让一处门控遗漏反过来改写已发布的观测契约。

## 实施与验证

`app/infrastructure/database.py` 的 `_run_probe` 改为 `if full and result[0] == "internal_error"`。完整探测行为不变：`probe()` / `init()` 仍置 `unavailable` 并抛脱敏的 `DatabaseRuntimeError`。

- 定向：`uv run pytest tests/unit/test_database.py tests/unit/test_database_lifecycle_review.py -q` → 46 passed（新增 1 条）。
- 全量：`uv run pytest -q` → 1628 passed, 5 xfailed。

## 边界与关联

本轮只闭合观测路径。另一条 P3（`_reason` 把运行期自身抛出的 `closing` / `close_incomplete` 归为未知程序缺陷，使 `probe()` / `init()` 抛 `internal_error` 而非返回状态）是独立根因，改的是 DB-ADR-001 D5 的白名单口径，仍待修复。

`ping()` 的契约正文维护在 [DB-ADR-001 D4](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md#d4运行时与故障语义) 与[组件文档](../../../docs/infrastructure_doc/database_doc/database.md#内部协作契约)的契约表。提交前审查记录引用的是 infrastructure.md，该文件的接口与内部协作契约已在 DB-F02 文档收敛中移至组件文档，原引用按当时正文来源保留，不在本轮改写。
