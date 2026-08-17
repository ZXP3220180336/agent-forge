# 执行统计（ToolStats / ToolStatsCollector）说明文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/stats.py`
> **职责**：工具执行统计 —— 调用次数 / 成功率 / 平均耗时 / 最后调用时间，全量摘要
> **状态**：✅ 已实现

---

## 📋 目录

- [定位与职责](#定位与职责)
- [接口契约](#接口契约)
- [行为边界](#行为边界)
- [设计决策](#设计决策)
- [测试](#测试)
- [相关文档](#相关文档)

---

## 定位与职责

ToolStatsCollector 记录每个工具的执行统计（由 [executor.md](executor.md) 在每次真实尝试后调用 `record`），供监控 / 调试 / 管理界面查询。统计按注册 key（`tool.name`）独立存储。

## 接口契约

### `ToolStats`（`@dataclass`）

| 字段 / 属性 | 类型 | 说明 |
| --- | --- | --- |
| `call_count` | `int` | 调用次数 |
| `success_count` / `failed_count` | `int` | 成功 / 失败次数 |
| `total_time` | `float` | 总耗时（秒） |
| `last_call_time` | `float \| None` | 最后调用时间戳 |
| `success_rate` | `property` | 成功率 = `success_count / call_count` |
| `avg_time` | `property` | 平均耗时 = `total_time / call_count` |

### `ToolStatsCollector`

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `init` | `(name: str) -> None` | 注册工具时初始化统计条目（幂等） |
| `remove` | `(name: str) -> None` | 注销工具时删除统计条目 |
| `record` | `(name, success: bool, elapsed: float) -> None` | 记录一次真实尝试；未注册工具名惰性建条目 |
| `get` | `(name=None) -> dict \| ToolStats \| None` | 单工具（name 给定）或全量字典 |
| `summary` | `() -> dict` | 全量摘要 |

### `summary()` 返回结构

```text
{
  "total_calls": int, "total_success": int, "total_failed": int,
  "overall_success_rate": float,
  "tools": { "search": {"call_count", "success_rate", "avg_time", "last_call_time"}, ... }
}
```

## 行为边界

| 场景 | 行为 |
| --- | --- |
| 零调用查询 | `success_rate` / `avg_time` 返回 0.0（不除零） |
| 记录未注册工具 | 惰性创建 `ToolStats()` 条目 |
| 注销工具 | 统计条目同步删除 |

## 设计决策

- 统计为**尽力而为**：并发下同步字典更新（无锁），多任务并发时数值可能不精确；`last_call_time` 用 `time.time()` 墙上时钟（非单调时钟）
- 统计仅做采集与查询，不做持久化（重启重置）→ [ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md)

## 测试

`tests/unit/test_tools.py`（`test_tool_service_execute_basic` 断言 `call_count` / `success_count`）；executor 路径间接触发 `record`（统计断言见 test_tools.py）。

## 相关文档

- [executor.md](executor.md)（统计记录调用方）
- [tool_service.md](tool_service.md)（`get_stats` / `get_all_stats_summary` 入口）
- [工具模块接口文档](tools.md)
