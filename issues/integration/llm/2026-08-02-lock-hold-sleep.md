# TokenBucket 持锁 sleep：等待期间锁被持有，阻塞其他请求、无法响应取消

> **状态**：✅ 已修复（2026-08-02，连带解决问题 6）
> **优先级**：P1（中）
> **来源**：2026-08-01 代码审核（问题 2 + 6）· 2026-08-16 从 limiter.md 提取归档
> **涉及模块**：`app/integration/llm/reservation_limiter.py`（`TokenBucket.acquire`）
> **关联文档**：[limiter.md](../../../docs/integration_doc/llm_doc/limiter.md)

---

## 问题描述

### 现象

`TokenBucket.acquire` 的等待逻辑在 `while True` 循环内持有锁 sleep——其他排队请求无法并行计算等待时间；长 sleep 阻塞短等待请求；sleep 期间无法响应取消。

连带问题 6：扣减 token 处 `_tokens` 可轻微为负（扣减逻辑不严）。

### 影响

持锁 sleep 阻塞并行（理论缺陷，实测单桶未放大）；负 token 记账不精确。

### 根因

「持锁等待」模式——锁内完成计算 + sleep。

---

## 工业级参照

| 库 | 等待模型 | 持锁等待？ |
| --- | --- | --- |
| Go `x/time/rate` | `reserveN` 锁内记账 → `Wait(ctx)` 锁外 | 否 |
| Guava `acquire()` | `reserve()` 锁内记账 → 锁外 sleep | 否（源码注释确认刻意设计） |
| Bucket4j | 非阻塞 `tryConsume()` | 否 |

**工业级共识：锁内只做记账，锁外等待。**

---

## 修复方案（含决策取舍）

**决策**：`TokenBucket.acquire` 重构为「锁内计算 → 锁外 sleep → 循环重检」——锁内仅计算 `wait_time`，`asyncio.sleep` 移锁外，醒来回循环顶部重检（sleep 期间 token 可能被抢走，重检保证公平且不过等）。

**取舍理由**：

1. 与 Guava `reserve` + 锁外 sleep 同构——锁只做记账，等待让出事件循环；
2. 等待期间锁不被持有，其他请求可并行计算；sleep 可响应取消（`CancelledError` 正常传播）；
3. **连带解决问题 6**：新实现只在 `_tokens >= tokens` 时才扣减，不再出现负 token。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/reservation_limiter.py` | `acquire` 重构锁外 sleep + 循环重检 | `test_bucket_wait_does_not_block_others` / `test_bucket_cancel_does_not_corrupt_state` |

---

## 验证

- 短等待不被长等待阻塞；取消不破坏桶状态；`_tokens` 不再为负
- 全量测试通过（2026-08-02 修复时验证）

---

## 教训沉淀

- **限流等待必须锁外**：锁内只做记账（reserve），锁外等待（sleep）——这是 x/time/rate、Guava 等工业实现的共识；持锁 sleep 阻塞并行、无法响应取消。
- **仅在满足条件时扣减**：`_tokens >= tokens` 才扣减，避免负 token 记账。
