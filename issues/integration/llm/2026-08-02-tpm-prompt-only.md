# TPM 桶只算 prompt token，输出大时低估实际消耗

> **状态**：✅ 已修复（2026-08-02；自适应预留 2026-08-06）
> **优先级**：P1（中）
> **来源**：2026-08-01 代码审核（问题 3）· 2026-08-16 从 limiter.md 提取归档
> **涉及模块**：`app/integration/llm/llm_service.py`（`_count_prompt_tokens`）
> **关联文档**：[limiter.md](../../../docs/integration_doc/llm_doc/limiter.md)

---

## 问题描述

### 现象

`estimated_tokens` 不含输出 token（completion）——输出大时 TPM 桶低估实际消耗。

### 影响

TPM 限流偏宽松，输出 token 大的调用超扣服务端限额。

### 根因

估算口径只算 prompt，未加输出余量。

---

## 工业级参照

| 方案 | 输入侧 | 输出侧 |
| --- | --- | --- |
| OpenAI/Azure 官方 | 提交时估算 | `estimated = prompt + max_tokens × best_of` |
| LiteLLM | tiktoken 精确 | 预留有效输出上限 |
| Fenic | tiktoken 精确 | p95 自适应预留 × 1.15 |

**工业级共识：TPM 官方口径 = 输入 + 输出都计入；单次预估值 = prompt + max_tokens（输出上限）。只算 prompt 是已知严重低估。**

---

## 修复方案（含决策取舍）

**决策**：`_count_prompt_tokens(model_key, messages, max_tokens)` 新增第三参，估算 = prompt tokens + `max_tokens`（输出上限保守余量）；TPM 桶按「请求可能消耗的最大 token」扣减，宁可高估不错放。

**修复要点**：

1. 估算 = prompt + `max_tokens`；`async_generate()`/`generate()` 两处传各自实际 `max_tokens`；签名向后兼容（默认 0 = 旧口径）；
2. **结算退差（已实现）**：`reserve()` → `Reservation`，请求完成后 `settle(actual)` 退 TPM 差——高估预留事后退回；
3. **自适应预留（2026-08-06，根治方向）**：`OutputTokenEstimator` 用历史实际输出高分位（普通 p95/推理 p99）× 安全系数 1.15 替代固定 `max_tokens` 预留，clamp 只减不加；减少预留期间占桶（并发空耗）——详见 ADR「自适应预留（Fenic 式）」；
4. **结构性解耦**：provider `max_tokens` 保持宽裕不截断，只有限流器预留下降。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/llm_service.py` | `_count_prompt_tokens` 加 `max_tokens` 输出余量 | `test_reservation_limiter.py` 预留/退差用例 |
| `app/integration/llm/reservation_limiter.py` | `Reservation` settle 退差 + `OutputTokenEstimator` 自适应 | `test_reservation_limiter.py` 自适应预留用例 |

---

## 验证

- 预留 = prompt + max_tokens；settle 退差；自适应预留减少占桶
- 全量测试通过（2026-08-02 + 08-06 验证）

---

## 教训沉淀

- **TPM 估算必须含输出**：官方口径输入 + 输出都计入，只算 prompt 严重低估——加 `max_tokens` 上限余量 + 结算退差。
- **「预留期间占桶」靠自适应分布预留根治**：退差只能事后释放，预留期间按上限占桶导致并发空耗——用高分位分布估算替代静态上限（Fenic 式）。
