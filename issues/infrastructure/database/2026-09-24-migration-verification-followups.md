# 命令原因码语义与行为边界表述不符，且收尾/终态分支缺断言

> **ID**：DB-015 · **日期**：2026-09-24 · **状态**：已修复 · **优先级**：P2
> **来源/范围**：DB-F03b 未提交工作区的三路只读验证（CLI 生命周期、迁移核心增量、文档登记一致性）；未发布，未执行真实迁移。

## 现象与影响

验证在已实现但未提交的 DB-F03b 工作区发现同一类问题：**终态与原因码的语义比文档或不变量更弱**，另有行为边界表述与实现不符。

1. **一次性 Owner 复用借用事务原因码**：`MigrationCommand.run` 的防复用守卫抛 `transaction_required`，但"命令对象被复用"与"连接不在真实事务/AUTOCOMMIT"无关，属调用方缺陷。这违反项目既有的"程序缺陷不伪装为可恢复故障"原则，且测试只断言 `MigrationError` 不校验码值。
2. **未发出的提交被记成提交结果未知**：`commit_started(version)` 位于提交前的 `_guard(operation_end)` 之前。若在该同步窗口耗尽预算，`uncertain_version` 已登记，异常处理便把"根本没发出 commit"报成 `commit_unknown`，与实际不符（过度保守，会误导运维去核对远端状态）。
3. **父进程终态标签覆盖"提交结果未知"**：`_supervise` 在强退分支置 `commit_unknown` 后，又用 `terminal_reason` 覆盖为 `cancelled`。事实未丢（`uncertain_version` / `forced_termination` / `commit_outcome_unknown` 仍在），但只看 `reason` 的调用方可能把"提交可能已落地"误读为"干净取消"，进而违反"提交未知后不得自动重试"。
4. **行为边界表述过强**：`migrations.md` 的原表把五种事务前置形态合并写成"不发出任何语句"，而驱动连接检查位于 `LOCK` 与历史读取之后——"仅逻辑事务""无驱动连接"两形态实际已发出锁与历史查询，与本仓库自己的用例断言（`calls == ["lock","history","raw"]`）矛盾。
5. **命令层原因码无正式枚举位置**：`MIGRATION_REASONS` 的 25 个取值（含父进程管道白名单）只在组件文档中作描述性提及，没有单一清单；`deployment.md` 只笼统写"`reason` 是固定原因码"。

影响集中在判定与可读性：不会造成错误写入，但会削弱"提交结果未知"这一运维关键信号的精度，并让文档与实现对不上。

## 根因

- 原因码被当作"就地可用的字符串"复用，没有按"调用方缺陷 → `internal_error`"的既有分类落位；
- 提交窗口的时间语义写在流程注释里，但登记时机早于提交请求，使"未知"的边界比事实宽；
- 父进程终态优先级只实现了"迟到消息不得覆盖"，没有覆盖"标签不得盖掉未知提交"这一方向；
- 文档表把不同阶段的拒绝合并成一行，粒度低于实现；
- 命令层原因码清单缺少唯一正文。

## 方案与取舍

1. 复用守卫改抛 `internal_error`，并在既有用例中断言码值；不新增原因码，不引入新异常类型。
2. 把 `commit_started` 移到提交前 `_guard` 之后：只有真正要发提交时才登记未确认版本。
3. 父进程仅在**未强退且没有未确认提交**时才用 `terminal_reason` 改写 `reason`；强退或存在未确认提交时保留 `commit_unknown`，`forced_termination` 继续记录强退事实。
4. 行为边界拆成两行，分别对应"发出任何语句之前拒绝"与"锁与历史读取之后、整批 SQL 之前拒绝"。
5. 命令层原因码清单以 `deployment.md` 为唯一正文并按分组枚举，组件文档改为链接，不复制第二份分类表。
6. 补齐验证缺口：收尾期限进入时已耗尽、收尾进行中被再次取消、驱动未关闭且终止失败、基线返回值不符契约、核心重复返回已确认版本、`Settings → database_config → MigrationTimeouts` 预算映射链路；预算映射提取为 `scripts.migrate._command_timeouts`，让键名漂移在测试中立即失败。

未采用的替代：为复用守卫新增专用原因码（会扩张原因码契约且无独立语义）；把 `commit_started` 保留在原位并改异常分支（需要区分"守卫超时"与"提交超时"，复杂度更高且更易漏）；为循环耗尽的 `for…else` 兜底补断言（需要构造违反核心契约的桩，收益低于失真成本，仍由核心的序列校验保证）。

## 实施与验证

红绿证据（均经 `uv run --no-sync`）：

| 检查 | 实际结果 |
| --- | --- |
| 先加用例 | `test_each_file_has_its_own_commit_and_noop_rolls_back`（码值）、`test_budget_exhausted_before_commit_is_timeout_not_unknown`、`test_keyboard_interrupt_cannot_hide_unknown_commit`、`test_command_budget_is_built_from_settings` |
| 未修复 | 4 failed、35 passed：分别为原因码不符、`commit_unknown` 应为 `timeout`、`cancelled` 应为 `commit_unknown`、`_command_timeouts` 尚不存在 |
| 修复后（迁移四个测试文件） | 109 passed |
| 迁移核心 + runtime + CLI + Session 生命周期 + Settings 专项 | 237 passed |
| 全量 `pytest -q` | 1749 passed、3 xfailed、1 条既有 Starlette/httpx 警告（60.42 秒） |
| 全库 Ruff check / format --check | 通过 |
| ALIGNMENT / 相对链接 / `git diff --check` | 通过 |

文档同步：`migrations.md` 行为边界拆行、命令生命周期补提交窗口/复用/终态优先级/门禁返回值契约、预算映射与命令层清单指向部署文档；`deployment.md` 更正过时引言并新增 `reason` 取值清单；`database.md` 契约表补 `configure_database_logging`；`ALIGNMENT.md` 同步两行描述；`todo.md` 修正评审归因并追加本轮结论。

未覆盖边界：真实 PostgreSQL 原子性、并发锁竞争、SIGTERM 强杀后服务端的隐式回滚、真实终端 Ctrl+C 端到端仍未验证，均归 DB-F03 整体与 DB-F06。

## 关联记录

[迁移核心契约](../../../docs/infrastructure_doc/database_doc/migrations.md) · [部署与验证](../../../docs/project/deployment.md#工具-schema-迁移) · [DB-013](2026-09-23-migration-cancel-cleanup.md) · [DB-014](2026-09-23-migration-terminal-preservation.md) · [G0-4/G0-6/G0-7](../../../docs/engineering/ai-engineering-rules.md#g0)
