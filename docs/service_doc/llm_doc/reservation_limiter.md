# ReservationLimiter 设计文档（reserve/settle 形态）

> **模块**：`app/services/llm/reservation_limiter.py`
> **职责**：LLM API 调用的客户端限流（RPM + TPM 双 Token Bucket，支持结算退差）
> **与 rate_limiter.py 的关系**：独立实现、不共用任何代码，仅 API 形态不同。对比见 [rate_limiter.md](rate_limiter.md)。
> **配套**：集成于 `LLMService.async_generate()` / `generate()`，见 `llm_service.py`

---

## 与 acquire 形态的区别

| 维度 | [rate_limiter.py](rate_limiter.md)（acquire） | reservation_limiter.py（reserve/settle） |
| --- | --- | --- |
| 核心 API | `await limiter.acquire(est)` → 返回等待时间 | `await limiter.reserve(est)` → `res.settle(actual)` |
| 结算能力 | 无（一次性扣减，不退款） | ✅ 结算退差（settle）/ 全额退（cancel） |
| 适用场景 | 不关心退差的简单调用 | 需按实际 usage 退还未用 TPM 配额 |
| 生产使用者 | 无（保留类供对比/兼容） | ✅ `llm_service.py` |

**为何要结算退差**：TPM 桶按 `prompt + max_tokens` 预留，实际输出往往远小于 `max_tokens`，长期偏保守低估可用量。reserve/settle 在请求完成后把未用完的配额退还给桶。工业级参照：Go `x/time/rate` Reservation、LiteLLM/Fenic 预留-结算协议。

---

## 组件详解

### `TokenBucket` — 单桶算法（自包含）

与 rate_limiter.py 的 TokenBucket 算法一致（锁外 sleep 循环重检、refill_rate<=0 防御），额外提供 `refund()`：

```python
async def refund(self, tokens: float = 1.0) -> None:
    """退还 token（受 capacity 封顶，best-effort）。"""
    async with self._lock:
        self._refill()
        self._tokens = min(self.capacity, self._tokens + tokens)
```

### `ReservationTokenBucket` — reserve 形态单桶

继承 `TokenBucket`，复用 acquire（等待/扣减/禁用判定）逻辑，新增 `reserve(tokens)` 返回 `Reservation`。

### `Reservation` — 预留对象（终态幂等）

```python
class Reservation:
    async def settle(self, actual: int | None) -> None:
        # actual=None 保留全部预留（保守）但标记终态；actual>=0 退 max(0, reserved-actual)
    async def cancel(self) -> None:
        # 全额退还 reserved（请求未确认发出时用）
    @property
    def settled(self) -> bool: ...
```

**语义**：
- `settle(actual)`：请求完成后按实际消耗结算，退还 `max(0, reserved - actual)`
- `settle(None)`：无 usage 时的保守路径——保留全部预留，但**标记终态**（闭环不泄漏）
- `cancel()`：请求未发出（create 失败/取消）时全额退还
- **终态幂等**：settle/cancel 任一调用后，再次调用为 no-op

### `ReservationLimiter` — 组合限流器

```python
class ReservationLimiter:
    async def reserve(self, estimated_tokens=0, retry_after=None) -> Reservation:
        # RPM 预留 1 + TPM 预留 estimated → 返回组合 Reservation
```

**组合 Reservation 语义（按「请求是否已发出」分界）**：

| 出口 | 方法 | 行为 |
| --- | --- | --- |
| create 失败/取消（请求未确认发出） | `cancel()` | 退 RPM 1 + TPM 全额 |
| create 成功后一切出口（整流/取消/成功） | `settle(actual)` | 退 TPM 差（`max(0, est-actual)`），RPM 不退 |

> **为何 settle 不退 RPM**：请求已真实发生，RPM 配额是真实消耗，退掉会让客户端以为自己有配额而服务端已超（触发 429）。cancel 只在「请求未确认发出」时退 RPM。

**防 R5（组合两步间硬取消）**：`reserve` 先扣 RPM 再扣 TPM，若 TPM 预留前被硬取消（`CancelledError`），`except BaseException` 回退已扣的 RPM。

### `ReservationLimiterManager` — 实例管理

与 `RateLimiterManager` 同款缓存模式：`_instances: dict[str, ReservationLimiter]`，`get()` 懒创建 + 缓存复用，`reset()` 清空。配置映射复用 `_RATE_LIMIT_FIELDS` 同款逻辑（main/reasoning/fast 各配 RPM/TPM）。

---

## 调用流程（llm_service.py 集成）

```
async_generate() / generate()
    │
    ├─ limiter = ReservationLimiterManager.get(model_key)
    ├─ active: dict[str, Reservation] = {}
    │
    ├─ retry.execute(call_fn=_rate_limited_call)
    │     └─ 每次 call_fn：
    │           res = await limiter.reserve(estimated_tokens=estimated)  # ← 预留配额
    │           active["res"] = res
    │           try:  await create(**kwargs)
    │           except BaseException:  res.cancel(); active.pop; raise   # 失败全额退
    │
    ├─ 各出口闭环（按"请求是否已发出"分界）：
    │     ├─ 成功 / 整流 / 取消 → res.settle(usage 的 total 或 None)     # 退 TPM 差
    │     └─ 硬取消（finally 兜底）→ res.cancel()                        # 全额退
    └─ 整流重试 → 重新进入 retry.execute → 再次 reserve
```

**统一闭环**：每个 reserve 必配结算——create 失败 `cancel()` 全额退，create 成功后一切出口 `settle(actual)` 退 TPM 差；迭代硬取消由 `finally` 兜底，无 reservation 泄漏。

---

## 配置项清单

与 rate_limiter.md 相同（`llm_main_rpm` / `llm_main_tpm` 等），经 `ReservationLimiterManager.get()` 从 settings 读取。

---

## 相关文档

- [rate_limiter.md](rate_limiter.md)（acquire 形态，对比用）
- [llm.md](llm.md)（LLM 层总览）
