# TOOLS-007 executor retry_count 成功/失败路径口径不一致

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P2（可观测性 / 审计留痕）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 重要项 2）
> **涉及模块**：`app/integration/tools/executor.py`（ToolExecutor._execute_with_retry）
> **关联文档**：[executor.md](../../../docs/integration_doc/tools_doc/executor.md) · [tools.md](../../../docs/integration_doc/tools_doc/tools.md)

---

## 问题描述

### 现象

同一「执行 3 次」场景：
    - **成功路径**（第 3 次尝试成功）`result.retry_count = attempt`——0 基循环索引，报**2**；
    - **全败路径**（3 次全败）`result.retry_count = actual_retries`——总尝试次数，报 **3**。

### 影响

审计留痕与证据链中 `retry_count` 口径随 success/failure 漂移：下游按「重试次数」或「尝试次数」解读必有一方错一，重试行为审计失真。

### 根因

成功路径用了 0 基循环索引 `attempt`，失败路径用累计执行次数 `actual_retries`——两条路径语义未统一。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 可观测性口径一致性（OpenTelemetry / Prometheus 语义约定） | 同一指标在成功 / 失败路径语义必须一致，否则聚合分析失真 |
| 计数器语义 | 区分「第几次尝试」（0 基索引）与「执行次数」（含首次）——指标应固定为后者 |

**核心**：执行次数（含首次）是唯一稳定口径；0 基索引只作循环内部细节，不外溢到结果元数据。

---

## 修复方案（含决策取舍）

**决策**：统一 `retry_count` = **实际执行次数（含首次）**：

| 路径 | 原实现 | 修复 |
| --- | --- | --- |
| 成功 | `retry_count = attempt`（0 基） | `retry_count = attempt + 1` |
| 全败 | `retry_count = actual_retries`（每轮 +1，已是执行次数） | 不变（与成功路径口径一致） |

两处均加注释固定口径（「第 1 次尝试 = 1，成功 / 失败路径口径一致」）。

**取舍理由**：

1. **执行次数是稳定语义**：不随成功 / 失败漂移，下游可统一解读；
2. **`attempt + 1` 最小改动**：不引入额外计数器（`actual_retries` 已有同语义变量），循环内 0 基索引保持原样。

**语义边界**：`retry_count` 语义 = 本次 execute 实际调用工具的次数（含首次成功或最终失败），工具重试/审计消费方统一按此解读。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/executor.py` | 成功路径 `result.retry_count = attempt + 1`；两处加口径注释 | `tests/unit/test_tool_executor_components.py` 新增 2 用例：`test_retry_count_success_path_is_executions`（前 2 次失败第 3 次成功 → 3）+ `test_retry_count_failure_path_is_executions`（3 次全败 → 3，口径一致） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) `ToolResult.retry_count` 注释改「实际执行次数（含首次，成功/失败口径一致）」；[tools.md](../../../docs/integration_doc/tools_doc/tools.md) 已写「实际尝试次数」无需改 | — |

---

## 验证

- 相关测试 **22 passed**（含 2 个新增口径一致性用例）
- 全量测试待提交前确认（增量改动：仅 retry_count 赋值，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **观测指标必须在成功 / 失败路径语义一致**：同一字段不同路径不同口径，聚合分析必然失真。
- **0 基循环索引不外溢为公开元数据**：内部 `attempt` 只作循环控制，对外暴露的计数必须是稳定语义（执行次数含首次）。
- **口径用注释固定**：元数据语义变更后，代码注释与 [tools.md](../../../docs/integration_doc/tools_doc/tools.md) 契约同步更新，避免后续再次漂移。
