# CostTracker 成本计算说明文档

> **模块**：`app/integration/llm/cost_tracker.py`
> **更新日期**：2026-08-15
> **职责**：按模型定价表估算 LLM 调用成本（单次 / 会话级累计）
> **状态**：✅ 已实现
> **配套**：`LLMService.calculate_cost()` 为 Facade 入口（见 [llm.md](llm.md)）

---

## 📋 目录

- [定位与职责](#定位与职责)
- [接口契约](#接口契约)
- [行为边界](#行为边界)
- [使用示例](#使用示例)
- [设计决策](#设计决策)
- [测试](#测试)
- [相关文档](#相关文档)

---

## 定位与职责

CostTracker 是系统的**成本计算器**：根据 Token 用量与模型定价，估算单次 LLM 调用成本，并支持会话级累计。

- **单次成本**：`calculate(usage, model)` → `{cost_usd, input_cost, output_cost}`
- **会话累计**：`accumulate(stats, cost)` 滚动累加
- **定价查找**：`_find_price(model)` 精确 → 最长前缀 → 默认，容忍模型版本号后缀

> **定价表（`MODEL_PRICING`）**：$/1K tokens，含 OpenAI / DeepSeek / Claude / Embedding 共 15 条，**以源码为准**（`app/integration/llm/cost_tracker.py`），随模型价格变动更新。未知模型回退默认均价 `DEFAULT_PRICE`（input 0.002 / output 0.008）。

## 接口契约

| 成员 | 签名 | 说明 |
| --- | --- | --- |
| `calculate` | `@staticmethod (usage: dict \| None, model: str = "") -> dict[str, float]` | 单次成本 `{cost_usd, input_cost, output_cost}`，round 6 |
| `accumulate` | `@staticmethod (stats: dict, cost: dict) -> dict[str, float]` | 会话级累计，round 6 |
| `_find_price` | `@staticmethod (model: str) -> dict[str, float]` | 定价查找（私有） |

`calculate` 公式：`input_cost = prompt_tokens / 1000 × 输入单价`；`output_cost = completion_tokens / 1000 × 输出单价`。

## 行为边界

| 场景 | 行为 |
| --- | --- |
| `usage=None` / 空 dict | 返回全 0，不抛异常 |
| `prompt_tokens` / `completion_tokens` 缺失或为 0 | `or 0` 兜底 |
| `model=""` / 未知模型 | 回退 `DEFAULT_PRICE` |
| 版本号后缀（`deepseek-chat-v2`） | 前缀匹配命中 `deepseek-chat` 定价 |

## 使用示例

```python
# 经 LLMService Facade（推荐，静态方法）
from app.integration.llm.llm_service import LLMService
cost = LLMService.calculate_cost(
    usage={"prompt_tokens": 500, "completion_tokens": 200},
    model="gpt-4",
)  # → {"cost_usd": 0.027, "input_cost": 0.015, "output_cost": 0.012}

# 直接使用 CostTracker（会话级累计）
from app.integration.llm.cost_tracker import CostTracker
session = {"cost_usd": 0.0, "input_cost": 0.0, "output_cost": 0.0}
for usage in all_usages:
    session = CostTracker.accumulate(session, CostTracker.calculate(usage, model))
```

## 设计决策

- **前缀匹配按 key 长度降序**（`sorted(keys, key=len, reverse=True)`）：保证**最长前缀优先**且结果不依赖字典插入顺序。未来定价表出现互为前缀的 key（如 `gpt-4o` / `gpt-4o-mini`）时行为仍确定。
- **定价表为模块内固定表**：改价 / 新增 key 走代码变更，由 `tests/unit/test_cost_tracker.py`（6 用例）回归防护（精确匹配优先 / 最长前缀 / 不依赖插入序 / 未知回退默认）。

## 测试

`tests/unit/test_cost_tracker.py`（6 用例）聚焦 `_find_price` 定价查找的确定性行为，作为行为契约的权威来源。

## 相关文档

- [LLM 层总览](llm.md)（`calculate_cost` Facade 入口）
- [Limiter 客户端限流](limiter.md)（成本与限流同属 LLM 治理层）
- [配置说明](../../config_doc/config.md)
