# 配置为 0 时除零崩溃（限流禁用表达）

> **状态**：✅ 已修复（2026-08-02）
> **优先级**：P0（严重，合并前必修）
> **来源**：2026-08-01 代码审核（问题 1）· 2026-08-16 从 limiter.md 提取归档
> **涉及模块**：`app/integration/llm/reservation_limiter.py`（`TokenBucket.acquire`）
> **关联文档**：[limiter.md](../../../docs/integration_doc/llm_doc/limiter.md)

---

## 问题描述

### 现象

RPM/TPM 配置为 0（或缺失）→ `TokenBucket(capacity=0, refill_rate=0)` → 桶空时 `wait_time = needed / 0` → `ZeroDivisionError`。已实测 `rpm=0` 时 `acquire` 直接崩溃。而「配置 0 表示禁用限流」是用户最自然的表达。

### 影响

配置 0（禁用限流）崩溃，限流不可用。

### 根因

`TokenBucket` 未防御 `refill_rate <= 0`。

---

## 工业级参照

| 库 | 0 的语义 | 禁用方式 |
| --- | --- | --- |
| Go `x/time/rate` | 0 = 不允许任何事件 | `rate.Inf` 允许所有 |
| Bucket4j | 无 0 特殊语义 | 极大值配置 + `enabled=false` |
| Traefik 事故 | 依赖 0-limit 禁用，2024 年 x/time 改语义后被破坏 | 改 `SetLimit(rate.Inf)` |

> 工业级「0 = 拒绝一切」，但本项目语境 `0` 只来自「配置未填」，**把 0 当禁用安全**（`capacity`/`refill_rate` 同源于 rpm/tpm 配置）。

---

## 修复方案（含决策取舍）

**决策**：`TokenBucket.acquire` 开头对 `refill_rate <= 0` 防御，直接放行返回 `0.0`——一个 guard 覆盖所有路径（`capacity`/`refill_rate` 同源为 0）。

**取舍理由**：

1. 「配置 0 = 禁用限流」是本项目语境下的安全语义（0 只来自配置未填，非业务禁止某模型）；
2. 一个 guard 覆盖 RPM/TPM 全路径，`RateLimiterManager.get` 无需改动；
3. 若未来需「某模型限流关闭但配置 0」，引入独立 `enabled` 开关（记录为可选）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/reservation_limiter.py` | `TokenBucket.acquire` 对 `refill_rate <= 0` 直接放行 | `test_reservation_limiter.py::test_bucket_zero_refill_disabled` |

---

## 验证

- `rpm=0`/`tpm=0` 配置立即放行、无等待、不崩溃
- 全量测试通过（2026-08-02 修复时验证）

---

## 教训沉淀

- **限流「禁用」的表达要防御**：`refill_rate <= 0` 直接放行，避免除零崩溃——本项目语境下「0 = 配置未填 = 禁用」安全；工业级更推荐显式 `enabled` 开关（语义无歧义）。
