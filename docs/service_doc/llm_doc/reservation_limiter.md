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

### `TokenBucket` — 单桶算法（纯 token 记账）

与 rate_limiter.py 的 TokenBucket 算法一致（锁外 sleep 循环重检、refill_rate<=0 防御），提供 `acquire()` 与 `refund()`：

```python
async def acquire(self, tokens: float = 1.0) -> float:
    """获取 Token，等待直到可用（返回等待秒数）。"""
    # 锁内计算 → 锁外 sleep → 循环重检

async def refund(self, tokens: float = 1.0) -> None:
    """退还 token（受 capacity 封顶，best-effort）。"""
    async with self._lock:
        self._refill()
        self._tokens = min(self.capacity, self._tokens + tokens)
```

> 桶本身只做 token 记账，**不感知预留/结算语义**——预留由上层 `Reservation` 持有条目、经 `refund` 结算。早期 `ReservationTokenBucket` 子类与 `TokenBucket.reserve()` 均已移除，预留编排全部上移到 `ReservationLimiter.reserve()`。

### `Reservation` — 预留对象（终态幂等）

**空对象构造**，由 `ReservationLimiter.reserve()` 逐桶 acquire 扣减后 `add()` 追加条目。以**条目列表**统一单桶 / 多桶组合：`entries: [(bucket, reserved), ...]`。语义约定：**首个条目为按次桶（RPM，settle 不退）**，其余条目为按量桶（TPM，settle 退差）。单桶 = 1 条目，双桶 = 2 条目。

```python
class Reservation:
    def __init__(self, settle_callback: Callable[[int], None] | None = None) -> None:
        # 空对象，条目由 add() 追加
        # settle_callback: settle(actual) 成功后喂实际消耗给自适应估算器（可选）
    def add(self, bucket, reserved) -> None:       # 追加桶到组合（按次之后追加按量）
    async def settle(self, actual: int | None) -> None:
        # 仅对按量桶（非首条目）退 max(0, reserved-actual)
        # actual=None 保留全部预留（保守）但标记终态
        # actual 非 None 且挂回调时 → 触发 settle_callback(actual)（喂估算器样本）
    async def cancel(self) -> None:
        # 所有桶全额退还（请求未确认发出时用）
    @property
    def settled(self) -> bool: ...
```

**语义**：

- `settle(actual)`：请求完成后按实际消耗结算，对按量桶退 `max(0, reserved - actual)`；按次桶不退
- `settle(None)`：无 usage 时的保守路径——保留全部预留，但**标记终态**（闭环不泄漏）
- `cancel()`：请求未发出（create 失败/取消）时**所有桶全额退还**
- **终态幂等**：settle/cancel 任一调用后，再次调用为 no-op

> 早期 `_CombinedReservation`（继承 Reservation 覆写 settle/cancel 处理双桶）已**合并进 `Reservation`**：改为空对象构造 + `add()` 追加条目表达组合，子类删除。

### `ReservationLimiter` — 组合限流器

```python
class ReservationLimiter:
    def __init__(self, rpm=60, tpm=100_000, *,
                 quantile=0.95, safety_margin=1.15, min_samples=30, window=256):
        # quantile/safety_margin/min_samples/window: 自适应预留估算器配置（懒创建）
        # _estimators: dict[int, OutputTokenEstimator]（按 max_tokens 分池）

    async def reserve(self, estimated_tokens=0, retry_after=None) -> Reservation:
        return await self._acquire(estimated_tokens, retry_after)  # 固定形态

    async def reserve_adaptive(self, prompt_tokens, max_tokens, retry_after=None) -> Reservation:
        # 自适应形态：预留 = prompt + estimate()，clamp 到 prompt + max_tokens
        # 挂 settle 回调：settle 时喂 actual_total - prompt 给估算器池

    async def _acquire(self, estimated_tokens, retry_after, settle_callback=None) -> Reservation:
        est = max(estimated_tokens, 1.0)
        res = Reservation(settle_callback=settle_callback)     # 空对象
        await self._req_bucket.acquire(1.0)                  # RPM 扣 1
        res.add(self._req_bucket, 1.0)                       # 首条目（按次桶，settle 不退）
        try:
            await self._token_bucket.acquire(est)            # TPM 扣 est
        except BaseException:
            await res.cancel()                               # 防 R5：TPM 预留前取消 → 回退 RPM
            raise
        res.add(self._token_bucket, est)                     # 按量条目
        return res
```

**组合实现**：`reserve`（固定形态）与 `reserve_adaptive`（自适应形态）都委托 `_acquire`。核心逻辑：空构造 `Reservation()`，RPM 桶 `acquire(1.0)` 扣减后 `res.add(req_bucket, 1.0)` 追加为首条目（按次桶），TPM 桶 `acquire(est)` 扣减后 `res.add(token_bucket, est)` 追加为按量条目。组合 Reservation 的 `settle` 只命中非首条目、`cancel` 命中全部条目。**防 R5（组合两步间硬取消）**：TPM 预留被取消时，`except BaseException` 回退已扣的 RPM（见下方「防 R5」）。

**组合 Reservation 语义（按「请求是否已发出」分界）**：

| 出口 | 方法 | 行为 |
| --- | --- | --- |
| create 失败/取消（请求未确认发出） | `cancel()` | 退 RPM 1 + TPM 全额 |
| create 成功后一切出口（整流/取消/成功） | `settle(actual)` | 退 TPM 差（`max(0, est-actual)`），RPM 不退 |

> **为何 settle 不退 RPM**：请求已真实发生，RPM 配额是真实消耗，退掉会让客户端以为自己有配额而服务端已超（触发 429）。cancel 只在「请求未确认发出」时退 RPM。

**防 R5（组合两步间硬取消）**：`reserve` 先扣 RPM 再扣 TPM，若 TPM 预留前被硬取消（`CancelledError`），`except BaseException` 回退已扣的 RPM。

### `OutputTokenEstimator` — 自适应输出估算器

**作用**：用「历史实际输出的高分位 × 安全系数」预测下一次预留的输出量，替代固定 `max_tokens` 上限——减少预留期间占桶（并发空耗）。Fenic 式设计，详见 [rate_limiter.md「对比 3.2」](rate_limiter.md#对比-32自适应预留fenic-式2026-08-06-实现)。

```python
class OutputTokenEstimator:
    def __init__(self, quantile=0.95, safety_margin=1.15,
                 min_samples=30, window=256): ...
    def record(self, actual_output_tokens: int) -> None   # settle 后喂实际输出
    def estimate(self) -> int   # 高分位×安全系数；冷启动返回 0（调用方回退静态上限）
    def reset(self) -> None     # 清空样本
```

- **滚动样本**：`deque(maxlen=window)`，默认 256 条，超限淘汰最旧（Fenic 同款）
- **分位数**：`sorted()` 排序取索引（nearest-rank，O(n log n)，n≤256，无 numpy）；普通模型 p95、推理模型 p99（`ReservationLimiterManager` 按 model_key 配置）
- **安全系数**：默认 1.15（可配 1.0~4.0），越高越保守、429 风险越低、吞吐越低
- **冷启动回退**：样本 < `min_samples`（默认 30）返回 0 → `reserve_adaptive` 回退静态 `max_tokens`
- **无锁**：record/estimate 均无 await 点，asyncio 单线程天然原子（与 TokenBucket 因 acquire 内 sleep 需锁不同）

### `reserve_adaptive` — 自适应预留

`ReservationLimiter` 新增的自适应形态：

```python
res = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
# 预留量 = prompt + estimate()，clamp 到 prompt + max_tokens（只减不加）
# settle 时自动把 actual_total - prompt 喂给对应 max_tokens 的估算器池
```

- **结构性解耦**：provider 仍收宽裕 `max_tokens`（不截断输出），只有限流器预留下降
- **settle 回调**：`Reservation` 挂回调，`settle(actual)` 成功时喂样本；`settle(None)`/`cancel()` 不记录（无真实 usage 不污染分布）
- **按 max_tokens 分池**：`_estimators: dict[int, OutputTokenEstimator]`，不同输出上限独立建模
- **开关**：`llm_adaptive_reserve`（默认关），开启后 `llm_service` 走 `reserve_adaptive`，`reserve(estimated)` 保留兼容

### `ReservationLimiterManager` — 实例管理

与 `RateLimiterManager` 同款缓存模式：`_instances: dict[str, ReservationLimiter]`，`get()` 懒创建 + 缓存复用，`reset()` 清空。配置映射复用 `_RATE_LIMIT_FIELDS` 同款逻辑（main/reasoning/fast 各配 RPM/TPM）。

**自适应预留分位数**：`_QUANTILE_FIELD_BY_KEY` 按 model_key 读取分位数配置——`main`/`fast` 用 `llm_reserve_quantile`（p95），`reasoning` 用 `llm_reserve_reasoning_quantile`（p99，推理输出有相关性突发尖峰）。`get()` 构造 limiter 时传入 `quantile` + 通用 `safety_margin`/`min_samples`/`window`。

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
    │           if llm_adaptive_reserve:                     # 自适应形态（默认关）
    │               res = await limiter.reserve_adaptive(prompt_tokens, max_tokens)
    │           else:                                        # 固定形态（默认）
    │               res = await limiter.reserve(estimated_tokens=estimated)
    │           active["res"] = res
    │           try:  await create(**kwargs)
    │           except BaseException:  res.cancel(); active.pop; raise   # 失败全额退
    │
    ├─ 各出口闭环（按"请求是否已发出"分界）：
    │     ├─ 成功 / 整流 / 取消 → res.settle(usage 的 total 或 None)     # 退 TPM 差 + 喂估算器样本
    │     └─ 硬取消（finally 兜底）→ res.cancel()                        # 全额退
    └─ 整流重试 → 重新进入 retry.execute → 再次 reserve（自适应每轮重新评估）
```

**统一闭环**：每个 reserve 必配结算——create 失败 `cancel()` 全额退，create 成功后一切出口 `settle(actual)` 退 TPM 差；迭代硬取消由 `finally` 兜底，无 reservation 泄漏。

---

## 配置项清单

与 rate_limiter.md 相同（`llm_main_rpm` / `llm_main_tpm` 等），经 `ReservationLimiterManager.get()` 从 settings 读取。另含自适应预留专属配置：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_ADAPTIVE_RESERVE` | `false` | 自适应预留开关（开启后用高分位估算输出） |
| `LLM_RESERVE_QUANTILE` | `0.95` | 普通模型输出分位数（p95） |
| `LLM_RESERVE_REASONING_QUANTILE` | `0.99` | 推理模型分位数（p99） |
| `LLM_RESERVE_SAFETY_MARGIN` | `1.15` | 安全系数（1.0~4.0） |
| `LLM_RESERVE_MIN_SAMPLES` | `30` | 冷启动阈值（样本不足用静态上限） |
| `LLM_RESERVE_WINDOW` | `256` | 滚动样本窗口（deque 上限） |

---

## 相关文档

- [rate_limiter.md](rate_limiter.md)（acquire 形态，对比用）
- [llm.md](llm.md)（LLM 层总览）
