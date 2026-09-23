# 半构建引擎会开放准入且丢失跟踪与脱敏

> **ID**：DB-007 · **日期**：2026-09-23 · **状态**：已修复 · **优先级**：P2
> **发现来源**：DB-F02 提交前审查（2026-09-22）第 1 项；尚未接入应用或发布。
> **范围**：`_build_engine` 的成员发布顺序与 `init()` 的重试路径。

## 现象与证据

`_build_engine` 先发布 `_engine`，监听器注册与工厂构造都排在其后。这段窗口内抛异常（三次 `event.listen`、logger filter 循环或 `async_sessionmaker` 构造）时，首个 `init()` 抛 `internal_error`，但 `_engine` 已保留、`_maker` 仍为 `None`。此后显式 `init()` / `probe()` 会跳过构建、探测成功并置 `ready`，工厂随之放行，调用工厂抛 `TypeError: 'NoneType' object is not callable`；同一次运行既没有池事件登记（`dispose()` 无法感知借出连接），也没有实例日志过滤器，config.md 声明的 echo 脱敏在该路径不成立。

红测 `tests/unit/test_database.py::test_half_built_engine_never_opens_admission`：让 `async_sessionmaker` 首次构造抛错。修复前断言 `runtime._engine is None` 失败（引擎已发布），修复后 `_engine` 与 `_maker` 都为 `None`，显式重试可完整重建并放行工厂。

## 根因与取舍

“引擎存在”被当作“构建完成”的判据，但发布点位于构建链中间，两者只在无异常时才等价。

采用方案：在局部变量里构造引擎与工厂，全部就绪后连续赋值发布——中间没有 await，从其他协程看是原子的。这样“`_engine` 非空 ⇔ 构建完成”，`_new_session` 里的断言前提随之成立；半构建失败只置 `unavailable`，显式重试从零重建。放弃的另一方向是失败时复位 `_engine`：主问题同样能修好，但它把“已发布再收回”当成常态，仍留下依赖调用方清理的中间态。

## 实施与验证

`app/infrastructure/database.py` 的 `_build_engine` 改为局部 `engine` / `maker`，末尾连续赋值 `self._engine` 与 `self._maker`；各步原注释保留。

- 定向：`uv run pytest tests/unit/test_database.py tests/unit/test_database_lifecycle_review.py -q` → 52 passed（新增 1 条）。
- 全量：`uv run pytest -q` → 1634 passed, 5 xfailed。
- `uv run ruff check .`、`uv run ruff format --check .` 与 `uv run python -m scripts.verify_alignment` 通过。

## 边界与关联

构建中途失败会在同名 logger 上留下已挂载的脱敏过滤器；重复挂载只做同一脱敏，不改变行为，本轮不处理。本记录关闭 DB-F02 提交前审查的最后一项，另三项见 [DB-004](2026-09-23-ping-admission-write.md)、[DB-005](2026-09-23-runtime-reason-classification.md)、[DB-006](2026-09-23-idle-driver-termination-guard.md)；脱敏契约正文在[配置参考](../../../docs/config_doc/config.md)，本记录不复写。
