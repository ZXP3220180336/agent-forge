# 定价查找策略：最长前缀匹配 + 模块内固定定价表

> **状态**：✅ 已采纳
> **决策日期**：2026-08-15（cost_tracker 子文档固化）
> **涉及模块**：`app/integration/llm/cost_tracker.py`（`CostTracker._find_price`）
> **关联文档**：[cost_tracker.md](../../../docs/integration_doc/llm_doc/cost_tracker.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## Context

- 成本计算需按模型 key 查定价。模型 key 存在**前缀关系**（如 `gpt-4o` / `gpt-4o-mini`），查表必须确定「最长前缀优先」——否则 `gpt-4o-mini` 可能误命中 `gpt-4o` 的定价。
- 定价表的存储形态有两种选择：外部配置（运行时可改）或模块内固定表（代码内常量）。
- 需求：查找行为**确定性**（同一 key 永远得到同一结果，不依赖字典插入顺序、不依赖环境）。

## Decision

**`_find_price` 按 key 长度降序做最长前缀匹配 + 定价表为模块内固定表。**

- **前缀匹配按 key 长度降序**（`sorted(keys, key=len, reverse=True)`）：保证最长前缀优先，且结果不依赖字典插入顺序——未来定价表出现互为前缀的 key 时行为仍确定。
- **定价表为模块内固定表**：改价 / 新增 key 走代码变更，由 `tests/unit/test_cost_tracker.py`（6 用例）回归防护（精确匹配优先 / 最长前缀 / 不依赖插入序 / 未知回退默认）。

## Consequences

- **正面**：查找行为完全确定（不依赖插入序/环境）；定价集中在模块内一处，测试作为行为契约的权威来源；未知 key 回退默认，不崩溃。
- **负面**：改价/新增 key 需代码变更 + 发布（非运行时配置）；若未来引入外部定价配置，需与固定表建立同步/覆盖机制（当前无此需求，保持简单）。
