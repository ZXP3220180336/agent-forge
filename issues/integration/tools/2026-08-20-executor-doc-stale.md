# TOOLS-042 executor.md 文档状态与代码不符（测试数 + 成功分支顺序）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档同步，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（A 类 #4 / #12）
> **涉及模块**：`docs/integration_doc/tools_doc/executor.md`
> **关联文档**：[executor.md](../../../docs/integration_doc/tools_doc/executor.md)

---

## 问题描述

### 现象

1. 测试状态写 `tests/unit/test_tool_executor_components.py`（**8 用例**），实际 **20 个**测试函数——漏列超时优先级三档、UNKNOWN 映射、prune_tool_lock、非 dict / 非 str 键 / 数组参数拒绝、retry_count 口径、max_retries=0 两例共 12 个用例。
2. 执行流程成功分支顺序写「成功 → `ResultProcessor.truncate_result` → 填 `execution_time` / `retry_count` → 统计 → 钩子」，代码实际**先赋值 execution_time / retry_count 再 truncate_result**（`executor.py:227-240`）——两操作互不依赖，仅序列描述与实现相反。

### 影响

测试用例数失真；成功分支执行顺序描述与代码相反，读者据此推演钩子看到的内容时会得出错误结论（边界情况 #6「截断先于统计/钩子」语义仍正确）。

### 根因

executor 组件测试随 TOOLS-006/007/008/015 持续扩充后文档未同步计数；文档以「概念序」而非实现序描述成功分支。

---

## 修复方案

- 测试状态改 20 用例，如实列出全部覆盖场景
- 成功分支改为实现序：填 execution_time / retry_count → truncate_result → 统计 → 钩子 → 返回

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `docs/integration_doc/tools_doc/executor.md` | 测试数 8→20 补场景；成功分支顺序修正 | 无（纯文档；executor 行为既有 20 用例覆盖） |

## 验证

- 全量 **542 passed**
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

## 教训沉淀

- **文档执行流程图应以实现序为准**：「概念序」描述会在读者推演钩子 / 截断时序时产生误导，尤其截断先后直接决定钩子看到的内容。
- **测试计数同步**：测试文件是文档「测试状态」节的唯一事实源。
