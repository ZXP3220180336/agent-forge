# TOOLS-008 executor max_retries=0 时 range(0) 零次循环，工具从未执行

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P2（边界条件 / 静默失败）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 重要项 3）
> **涉及模块**：`app/integration/tools/executor.py`（ToolExecutor.execute 重试参数解析）
> **关联文档**：[executor.md](../../../docs/integration_doc/tools_doc/executor.md) · [tools.md](../../../docs/integration_doc/tools_doc/tools.md)

---

## 问题描述

### 现象

`max_retries=0` 时 `for attempt in range(0)` 零次循环，直接落到「所有重试均失败」路径，返回 `success=False, error=None, content=""` 的静默空失败——工具从未执行，也无任何错误信息。

### 影响

调用方传 `max_retries=0` 意为「不重试（跑一次）」却得到「未执行」的误导性结果，难排查；`TOOL_MAX_RETRIES=0` 的全局配置同样触发。

### 根因

`max_retries` 语义为「总尝试次数」（默认 3 含首次），但未约束下界——`0` 产生零次循环，静默空失败。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 边界下界约束（防御性参数校验） | 次数类参数 clamp 到有效区间，禁止产生「零副作用」的合法值 |
| 语义澄清 | 「不重试」的合理表达 = 执行 1 次；0 次执行对工具调用无意义 |

**核心**：至少执行 1 次是工具调用的不变量；`max_retries=0` 应被解释为「不重试跑一次」，而非「跑零次」。

---

## 修复方案（含决策取舍）

**决策**：clamp 最小为 1（保持「`max_retries` = 总尝试次数」既有契约，改动最小）：

```python
max_retries = max_retries if max_retries is not None else self._tool_max_retries
max_retries = max(max_retries, 1)  # 至少执行一次：0 视为「不重试跑一次」
```

**取舍理由**（对比审查建议的另一方案「语义改为额外重试次数」）：

1. **不改契约**：现有 `max_retries` = 总尝试次数（默认 3 含首次）语义已被文档 / 测试 / 调用方依赖，改为「额外重试次数」会全局改变默认行为（3 → 4 次），影响面大；
2. **最小改动**：clamp 一行，边界从「静默零执行」变为「跑一次」，语义直观。

**语义边界**：`max_retries=0` 与全局配置 0 均视为「不重试跑一次」；正常值（≥1）行为不变。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/executor.py` | 重试参数解析后 `max_retries = max(max_retries, 1)` + 注释 | `tests/unit/test_tool_executor_components.py` 新增 2 用例：`test_execute_max_retries_zero_runs_once`（max_retries=0 工具执行 1 次、retry_count=1）+ `test_execute_max_retries_zero_failure_not_silent`（失败返回真实归因错误非静默空） |
| 文档 | [executor.md](../../../docs/integration_doc/tools_doc/executor.md) 重试节补「至少执行 1 次，max_retries=0 视为不重试跑一次」 | — |

---

## 验证

- 相关测试 **24 passed**（含 2 个新增 max_retries=0 用例；顺带修正 `_FlakyTool`/`_AlwaysFailTool` 测试工具缺 `content=""` 的隐藏缺陷）
- 全量测试待提交前确认（增量改动：仅参数 clamp，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **次数类参数必须约束下界**：合法但无副作用的值（0 次执行）比显式报错更隐蔽——clamp 到有效区间并固定语义。
- **「不重试」≠「执行零次」**：对工具调用而言，不重试的最小单位是执行一次；`max_retries=0` 应映射为「跑一次」。
- **测试工具也要守契约**：`ToolResult(success=False, error=...)` 缺必填 `content` 会抛 TypeError 被 executor 归为异常（掩盖业务失败重试语义）——测试桩同样要满足真实契约。
