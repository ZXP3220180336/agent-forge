# 事务前置条件读不到可选属性时落入 AttributeError

> **ID**：DB-012 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P3
> **来源/范围**：DB-F03a 迁移核心的 IDE 类型诊断复核；未发布，未接入 CLI 或应用。

## 现象与影响

`apply_next_migration` 直接从可选属性取值并链式调用：`connection.sync_connection.get_execution_options()` 与 `raw.driver_connection.is_in_transaction()`。

SQLAlchemy 2.0.51 中两者都是可选声明：

- `AsyncConnection.sync_connection: Optional[Connection]`（`ext/asyncio/engine.py`），异步方言可以不提供同步连接；
- `AsyncConnection.get_raw_connection()` 返回池代理，其 `driver_connection` 为 `Optional[Any]`（`pool/base.py`，方言以 `# type: ignore[override]` 保留与接口层的差异）。

取值为 `None` 时两条路径都抛 `AttributeError`（第 223、248 行）。影响不在数据写入，而在判定：无法核验事务这一真实结论被降级为"未知程序错误"，既不进稳定原因码集合，也让调用方无法按类别处理。

## 根因

把可选属性当必填使用——取值后没有证明非空就直接调用方法。这不是类型检查器误报：无同步连接的异步方言、不暴露驱动连接的池代理都是运行时可达分支，只是本项目当前的 asyncpg 方言恰好都会提供。

## 方案与取舍

在取值处判空并 fail closed：读不到执行选项就无法证明不是 AUTOCOMMIT，拿不到驱动连接就无法核验物理事务，两者都按 `transaction_required` 拒绝且不执行任何 SQL。

- 复用既有原因码，不新增码值，原因码表与组件文档契约不变；
- 不使用 `assert`：断言失败会变成未知程序错误，正是本问题要消除的归类；
- 不按"当前方言一定会提供"跳过检查，也不缓存取值结果。

## 实施与验证

两个取值点各自改为"判空 + 条件"的单条判断，并保留 fail closed 理由的注释；行为边界新增到组件文档。

红绿证据：

| 检查 | 实际结果 |
| --- | --- |
| 先加用例 | `test_requires_a_real_transaction[no_sync_connection]` 与 `test_missing_driver_connection_cannot_prove_a_transaction` |
| 未修复 | 分别以 `AttributeError: 'NoneType' object has no attribute 'get_execution_options'`、`... has no attribute 'is_in_transaction'` 失败 |
| 迁移核心专项 | 68 passed |
| `uv run --no-sync pytest -q` | 1708 passed、5 xfailed、1 条既有 Starlette/httpx 警告 |
| 全库 Ruff check / format --check | 通过；235 个文件格式通过 |
| ALIGNMENT / 相对链接 / `git diff --check` | 通过 |

组件文档已同步契约表前置条件、关键实现说明、行为边界与验证入口。仓库没有配置类型检查器依赖，诊断来自 IDE，本记录只主张运行时行为证据，不主张类型检查通过。

**可复用教训**：第三方库声明为可选的属性，要在受信入口判空并按稳定原因码 fail closed，不能让 `None` 走成属性访问错误——那会把"无法核验"伪装成程序缺陷。

## 关联记录

[迁移核心](../../../docs/infrastructure_doc/database_doc/migrations.md) · [DB-011](2026-09-23-migration-final-deadline-guard.md) · [DB-F03a 评审](../../../docs/history/completed-work.md#db-f03a-review)
