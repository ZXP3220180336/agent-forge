# LLM-010 settle/cancel 终态幂等存在并发竞态，可能重复退款

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 9）
> **涉及模块**：`app/integration/llm/reservation_limiter.py`（`Reservation.settle` / `cancel`）
> **关联文档**：[limiter.md](../../../docs/integration_doc/llm_doc/limiter.md) · [llm-005](2026-08-16-circuit-breaker-concurrency-tests.md)（asyncio 同步原语）

---

## 问题描述

### 现象

`Reservation.settle` / `cancel` 的 `_settled` 检查（`if self._settled: return`）与退款循环之间**有 await 点**（`await bucket.refund(...)`）。并发调用时两个协程都能通过检查进入退款循环 → **重复退款**，向桶注入本不存在的额度。

### 影响

类注释宣称的「终态幂等」契约不成立——当前生产路径串行（单请求内一个 res 只被 settle 或 cancel 一次）暂不触发；未来并发复用（如请求级并发结算）即出现配额超发。`TokenBucket.refund` 的 capacity 封顶只防「桶超容量」，不防「容量以下的重复虚增」。

### 根因

终态判定（`_settled` 检查）与退款执行（含 await）非原子——asyncio 单线程下协程在 await 点交错，检查通过后其他协程也能进入。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| CPython asyncio 同步原语 | 单线程但协程在 await 点交错，共享状态非自动安全；`asyncio.Lock` 保证临界区互斥（llm-005 已调研） |
| token-throttle | 分布式退款用 Redis 幂等键保证「退款只应用一次」——本地单进程用互斥锁等价 |

**核心**：终态操作（settle/cancel）需要互斥，保证并发调用只有一个执行退款循环。

---

## 修复方案（含决策取舍）

**决策**：`Reservation` 增加 `asyncio.Lock`，`settle` / `cancel` 的检查 + 退款循环整体放入 `async with self._lock`。

**取舍理由**：

1. `asyncio.Lock` 是共享状态互斥的标准原语（llm-005 已确认）；
2. 锁粒度覆盖「检查 + 退款 + 置终态」整个临界区——并发调用串行化，第二个看到 `_settled=True` 直接返回，不重复退款；
3. 每个 Reservation 独立锁（构造时创建），无跨实例竞争，开销可忽略。

**语义边界**：

- 正常串行路径行为不变（锁获取/释放开销极小）；
- 并发 settle/cancel：先到者执行退款，后到者看到 `_settled` 直接 return；
- `settle` 中途被取消（CancelledError）→ 锁释放，res 未终态，外层兜底可重入（`_settled` 检查在锁内仍有效）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/reservation_limiter.py` | `Reservation` 加 `_lock`（`__slots__` + `asyncio.Lock`）；`settle`/`cancel` 检查 + 退款循环放 `async with self._lock` | `test_reservation_limiter.py` 新增 `test_concurrent_settle_cancel_mutex_no_double_refund` |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表加 LLM-010 条目） | — |

---

## 验证

- `test_reservation_limiter.py` / `test_stream_rectify.py` **57 passed**（含新增并发互斥用例）
- 全量测试 **363 passed**（46.81s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **「终态幂等」必须覆盖整个临界区**：`_settled` 检查与退款循环之间若有 await，并发调用即重复退款——终态操作需互斥锁（`asyncio.Lock`），或原子状态机。
- **capacity 封顶不防重复虚增**：`refund` 的 capacity 封顶只防桶超容量，容量以下的重复退款仍注入不存在的额度——幂等必须靠互斥而非封顶。
