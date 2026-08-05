# RateLimiter 设计文档

> **模块**：`app/services/llm/rate_limiter.py`
> **职责**：LLM API 调用的客户端限流（RPM + TPM 双 Token Bucket）
> **reserve/settle 形态**：见 [reservation_limiter.md](reservation_limiter.md)（独立文件，不共用代码）
> **配套**：集成于 `LLMService.async_generate()` / `generate()`，见 `llm_service.py`

---

## 目录

- [设计目标](#设计目标)
- [核心概念解释](#核心概念解释)
  - [限流（Rate Limiting）](#限流rate-limiting)
  - [Token Bucket 算法](#token-bucket-算法)
  - [其他限流算法](#其他限流算法)
  - [RPM / TPM](#rpm--tpm)
  - [Retry-After 响应头](#retry-after-响应头)
  - [突发（Burst）与平滑（Smoothing）](#突发burst与平滑smoothing)
- [架构总览](#架构总览)
- [组件详解](#组件详解)
  - [TokenBucket — 单桶算法](#tokenbucket--单桶算法)
  - [RateLimiter — 双桶组合](#ratelimiter--双桶组合)
  - [RateLimiterManager — 实例管理](#ratelimitermanager--实例管理)
- [调用流程](#调用流程)
- [与重试/熔断的分层配合](#与重试熔断的分层配合)
- [配置项清单](#配置项清单)
- [已知边界与设计取舍](#已知边界与设计取舍)
- [工业级对比：修复方案 vs 主流实现](#工业级对比修复方案-vs-主流实现)
- [附录：2026-08-01 代码审核记录](#附录2026-08-01-代码审核记录)

---

## 设计目标

1. **保护配额**：在到达服务商硬限额前主动限流，避免 429 触发重试风暴
2. **允许突发**：Agent 场景下工具调用常集中在一小段时间，限流器要能处理瞬时尖峰
3. **按模型独立**：main / reasoning / fast 三套模型配额不同，各自独立记账
4. **透明等待**：限流是排队而非拒绝——请求等待配额，不失败，对上层无感

---

## 核心概念解释

### 限流（Rate Limiting）

控制单位时间内的请求量或消耗量，防止超过服务商配额（如 DeepSeek 2M tokens/分钟）。**限流 vs 重试**：

- **限流**是**事前**（proactive）：请求发出前检查本地配额，够才发
- **重试**是**事后**（reactive）：请求已发出、失败后补救

本模块做的是客户端限流（client-side），与服务商网关限流互补。

### Token Bucket 算法

令牌桶：桶里攒 Token，请求取走 Token，Token 以固定速率补充。

```
capacity      桶容量 = 最大突发量
refill_rate   每秒补充速率
_tokens       当前剩余 Token（初始 = capacity）

请求 → 需要 tokens 个 Token
  桶里够  → 直接扣除，立即放行
  桶里不够 → 等待 (needed / refill_rate) 秒补足，再扣
```

**为什么选 Token Bucket**（对比见 `llm.md`「限流算法」节）：

| 算法 | 突发能力 | 平滑性 | 内存 |
| --- | --- | --- | --- |
| **Token Bucket** | ✅ 允许突发 | 长期平滑 | 常数 |
| 漏桶（Leaky） | ❌ 恒定速率 | 严格整形 | 常数 |
| 固定窗口 | 窗口边界双倍 | 一般 | 常数 |
| 滑动窗口日志 | — | 精确 | 随窗口增长 |

Token Bucket 在「允许突发」和「长期平滑」间取得平衡，适合 Agent 的突发调用模式。

### 其他限流算法

限流算法不止 Token Bucket 一种。按「是否允许突发、是否精确、实现复杂度」的维度，主流算法可归为几类：

```
               允许突发？      平滑性        内存/复杂度       典型实现
Token Bucket     ✅             长期平滑        常数           x/time/rate, Bucket4j
漏桶 Leaky       ❌（恒定速率）   严格整形        常数           queue + 定时出队
固定窗口         窗口边界双倍     一般            常数           Nginx limit_req（早期）
滑动窗口日志     精确            精确            随窗口增长     直方图/Redis ZSET
滑动窗口计数     部分（分桶粒度） 接近平滑         常数（分桶）   Redis INCR + 时间桶
GCRA             ✅             精确            常数           rate-limit 库（Ruby）
```

各算法要点与适用场景：

#### 固定窗口（Fixed Window）

按固定时间窗（如 1 分钟）计数，窗口内计数达到上限即拒绝。

```
窗口 [0,60) 计数 55 → 剩余 5
窗口 [60,120) 计数 40 → 剩余 20   ← 窗口切换，计数清零
```

- **优点**：实现最简单（一个计数器 + 窗口边界判断），内存常数
- **缺点**：**窗口边界双倍请求**——若 59s 用了 55 个、61s 又用 55 个，两秒内实际发了 110 个（两倍配额），跨窗口交界突发无防护
- **适用**：对瞬时峰值不敏感、实现优先的场景；Nginx 早期 `limit_req`、Redis 简单计数

#### 滑动窗口日志（Sliding Window Log）

记录窗口内每次请求的时间戳，查询时剔除过期时间戳后计数。

```
窗口 [T-60, T)：每个请求一个时间戳，滑动删除超窗的
请求到来 → 删除 < T-60 的 → 计数 → 超限拒绝
```

- **优点**：**最精确**——任意时刻窗口内的请求数都真实反映，无边界双倍问题
- **缺点**：**内存随窗口内请求量增长**（每个请求一个时间戳）；高吞吐下内存和淘汰开销大
- **适用**：低 QPS 但要求精确的场景；Redis ZSET（Sorted Set）实现

#### 滑动窗口计数（Sliding Window Counter）

固定窗口 + 细粒度分桶的折中：把窗口切成 N 个小桶，滑动时按比例加权历史桶计数。

```
窗口 = 4 个 15s 小桶，当前桶按剩余时间加权：
current + 上一桶 × (剩余比例) = 近似窗口计数
```

- **优点**：内存常数（N 个桶），比固定窗口平滑（无边界双倍），比滑动窗口日志省内存
- **缺点**：分桶粒度内仍不精确（边界内的小幅突刺）
- **适用**：Redis 分桶实现（`INCR + EXPIRE`）、Cloudflare 的限流方案；多数生产网关（API Gateway）的实际选择

#### 漏桶（Leaky Bucket）

恒定速率流出：请求先入队（桶），以固定速率逐出执行；桶满则拒绝（或丢弃）。

```
         ┌──────────────┐
 请求 →  │   漏桶（队列）  │ → 恒定速率流出
         └──────────────┘
  桶满 → 拒绝新请求（队尾丢弃）
```

- **优点**：输出速率**严格恒定**，天然整形流量；内存常数
- **缺点**：**无法应对突发**——即使桶是空的，新请求也按恒定速率流出；空闲期积攒的能力无法用于短时高峰
- **适用**：视频流、消息队列背压、需要严格平滑输出速率的场景（流量整形而非突发容忍）

#### GCRA（Generic Cell Rate Algorithm）

以「理论到达时间（TAT）」为核心的精确节流算法，常被视为 Token Bucket 的一种精确等价格式。

```
TAT = 上次请求的理论到达时间
新请求 → TAT' = max(now, TAT) + 1/rate
  若 TAT' - now > burst   → 拒绝（超出突发容忍）
  否则 → 接受，TAT = TAT'
```

- **优点**：**内存常数**（只存一个 TAT）+ 精确节流（无边界双倍）；单桶即可同时表达速率与突发上限
- **缺点**：概念上比 Token Bucket 抽象，理解成本略高
- **适用**：Ruby `rack/rate-limit`、部分 API Gateway；很多实现（如 x/time/rate 的 `advance`）本质等价于 GCRA

> **本模块为何选 Token Bucket**：Agent 场景（瞬时多个工具调用）需要**突发容忍**（排除固定窗口/漏桶），同时要**常数内存**（排除滑动窗口日志）。Token Bucket 与 GCRA 都满足，但 Token Bucket 的概念直观、与「桶/补充速率」的直觉一致，便于按 RPM/TPM 双桶独立建模。若未来需要更精确的边界控制，可评估滑动窗口计数或 GCRA。

### RPM / TPM

| 指标 | 全称 | 含义 | 本模块桶 |
| --- | --- | --- | --- |
| RPM | Requests Per Minute | 每分钟请求次数 | `capacity=rpm, refill=rpm/60` |
| TPM | Tokens Per Minute | 每分钟 Token 消耗量 | `capacity=tpm, refill=tpm/60` |

双桶组合（`RateLimiter`）：一次请求同时扣 **RPM 桶 1 个** + **TPM 桶 estimated_tokens 个**。任一个桶不足都等待，两者都过才放行。

### Retry-After 响应头

服务端返回 429 时带的 `Retry-After` 秒数，指示客户端多久后重试。本模块 `acquire(retry_after=...)` 会**先** sleep 该时长，再扣双桶——尊重服务端反馈的退避建议，优先于本地估算。

### 突发（Burst）与平滑（Smoothing）

Token Bucket 的本质特征：

- **突发**：桶满（capacity）时，可一次性连续发出 capacity 个请求，不等待
- **平滑**：超过 capacity 后，请求只能按 refill_rate 的节奏放行，长期平均速率 = refill_rate

所以桶容量积攒的 token 可处理短时高峰，长期又不会超配额——这正是 Agent 场景（瞬时多个工具调用）想要的。

---

## 架构总览

```
LLMService（async_generate / generate）
        │
        ▼  每个 create 前
RateLimiterManager.get(model_key) ──→ 共享 RateLimiter 实例（缓存）
                                            │
                                            ▼
                                        RateLimiter（双桶）
                                      ┌────────────┬────────────┐
                                      ▼            ▼            │
                                RPM 桶        TPM 桶    retry_after 等待
                            capacity=rpm  capacity=tpm      （最先）
```

**分层**：

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 管理 | `RateLimiterManager` | model_key → 限流器实例的缓存映射 |
| 组合 | `RateLimiter` | RPM + TPM 双桶 + Retry-After 编排 |
| 算法 | `TokenBucket` | 单桶 Token Bucket 算法本体 |

---

## 组件详解

### TokenBucket — 单桶算法

```python
class TokenBucket:
    def __init__(self, capacity, refill_rate):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens=1.0) -> float:
        # 配置 0 = 禁用限流：refill_rate <= 0 时无限速率直接放行，避免除零崩溃
        if self.refill_rate <= 0:
            return 0.0

        # 锁内计算 → 锁外 sleep → 循环重检（见「已知边界」边界 1）
        total_wait = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return total_wait
                wait_time = (tokens - self._tokens) / self.refill_rate

            # 锁外 sleep：期间其他请求可获取锁、扣减 token
            await asyncio.sleep(wait_time)
            total_wait += wait_time
            # 回到循环顶部重新检查——sleep 期间 token 可能被其他请求抢走

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now
```

**要点**：

- **时间源**：`time.monotonic()`（单调时钟），不受系统时间调整影响
- **惰性补充**：`_refill()` 只在 acquire 时调用，无需后台定时器，按经过时间换算补 token
- **容量封顶**：`min(capacity, ...)`，闲置期间不会无限积累 token，突发上限被约束
- **互斥**：`asyncio.Lock` 保证桶状态一致性（并发请求同时扣 token 不会超扣）；等待在锁外进行，锁不阻塞其他请求、可响应取消
- **配置 0 语义**：`refill_rate <= 0` 直接放行（配置 0 = 禁用限流，见「已知边界」边界 4）

**返回语义**：`acquire` 返回**桶内累计等待时间**（秒），桶充足时立即返回 `0.0`。

### RateLimiter — 双桶组合

```python
class RateLimiter:
    def __init__(self, rpm=60, tpm=100_000):
        self._req_bucket = TokenBucket(capacity=rpm, refill_rate=rpm / 60)
        self._token_bucket = TokenBucket(capacity=tpm, refill_rate=tpm / 60)

    async def acquire(self, estimated_tokens=0, retry_after=None) -> float:
        if retry_after:
            await asyncio.sleep(retry_after)   # 1. 服务端退避建议
        wait1 = await self._req_bucket.acquire(1.0)                 # 2. RPM 桶
        wait2 = await self._token_bucket.acquire(max(estimated_tokens, 1.0))  # 3. TPM 桶
        return wait1 + wait2
```

**要点**：

- **RPM 桶固定扣 1**：每次请求 = 1 次调用
- **TPM 桶按 `estimated_tokens` 扣**：请求前预估的 token 消耗 = prompt + `max_tokens` 输出余量（`max(..., 1.0)` 防止 0 造成桶不扣）
- **固定顺序**：先 RPM 后 TPM，无锁竞争死锁
- **`retry_after` 在最前**：不持桶锁，让所有请求统一遵守服务端退避

### RateLimiterManager — 实例管理

```python
_RATE_LIMIT_FIELDS = {
    "main": ("llm_main_rpm", "llm_main_tpm"),
    "reasoning": ("llm_reasoning_rpm", "llm_reasoning_tpm"),
    "fast": ("llm_fast_rpm", "llm_fast_tpm"),
}

class RateLimiterManager:
    _instances: ClassVar[dict[str, RateLimiter]] = {}

    @classmethod
    def get(cls, model_key="main") -> RateLimiter:
        # 缓存命中 → 返回；未命中 → 按 settings 懒创建 + 缓存
        # 未知 key → ValueError
    @classmethod
    def reset(cls) -> None:
        cls._instances.clear()
```

**要点**：

- **共享实例**：同一 model_key 复用同一个 `RateLimiter`——**双桶必须跨请求记账**，每次 new 会重置桶、等于没限流
- **懒加载**：首次 `get()` 才创建，读取 `settings` 的 RPM/TPM 配置
- **同步无竞态**：`get` 无 await，GIL 下天然原子，不会双实例
- **`reset()`**：配置变更或测试时清空缓存

---

## 调用流程

```
async_generate() / generate()
    │
    ├─ estimated = _count_prompt_tokens(model_key, messages, max_tokens)  # prompt + 输出余量，循环外一次
    ├─ limiter = RateLimiterManager.get(model_key)
    │
    ├─ retry.execute(call_fn=_rate_limited_call)               # 重试/熔断/fallback
    │     └─ 每次 call_fn（原始 + 重试）：
    │           await limiter.acquire(estimated_tokens=estimated)  # ← 限流
    │           │  桶足 → 立即放行（0 等待）
    │           │  桶空 → sleep 等待补充（期间事件循环放行其他任务）
    │           └─ retry_after 存在 → 先 sleep(retry_after)
    │           await client.chat.completions.create(**kwargs)     # 真实请求
    │     fallback（备用模型）：不 acquire，直接降级调用
    │
    ├─ async for chunk in response:  ...                        # 流式解析
    └─ (整流重试 → 重新进入 retry.execute → 再次 acquire)
```

**关键点**：acquire 位于 call_fn 内部，**每次真实请求**（原始调用、retry 内部重试、整流重试）都重新 acquire。整流重试每轮重新进入 `retry.execute`，再次 acquire；测试已断言 `calls["acquire"] == 2`（整流 2 轮）。

> 注：本文件的 acquire 形态为独立实现；llm_service.py 实际使用的是 reserve/settle 形态（见 [reservation_limiter.md](reservation_limiter.md)）。

---

## 与重试/熔断的分层配合

```
 acquire()      create()       429 时 retry 内部
客户端预限流    正式请求      服务端反馈限流
```

- **限流（acquire）**：事前本地排队，配额不足时等待而非请求
- **重试/熔断（retry.execute）**：事后处理下游故障；对 429 尊重 `Retry-After` 退避
- **两者互补**：客户端限流减少触发服务端 429；真遇到 429，重试层兜底

**注意**：acquire 的 `retry_after` 与 retry 层的 `Retry-After` 是两条独立路径——前者是调用方显式传入的等待，后者是 429 异常响应头内提取的退避。

**跨模块问题**：限流与重试的配合（重试/降级是否计入限流申请）详见 [llm.md「配额缺口」章节](llm.md#配额缺口重试降级不计入限流申请)。

---

## 配置项清单

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `llm_main_rpm` | int | `60` | 主模型每分钟请求数 |
| `llm_reasoning_rpm` | int | `30` | 推理模型每分钟请求数 |
| `llm_fast_rpm` | int | `100` | 快速模型每分钟请求数 |
| `llm_main_tpm` | int | `2000000` | 主模型每分钟 Token 消耗量 |
| `llm_reasoning_tpm` | int | `2000000` | 推理模型每分钟 Token 消耗量 |
| `llm_fast_tpm` | int | `2000000` | 快速模型每分钟 Token 消耗量 |

> TPM 默认值参考 DeepSeek 官方限额（2M tokens/分钟）。RPM/TPM 均需配合 `RateLimiterManager` 使用。

---

## 已知边界与设计取舍

1. **等待期间锁外 sleep**（`TokenBucket.acquire` 的 `while True` 循环）：「锁内计算 → 锁外 sleep → 循环重检」，等待期间锁不被持有，其他请求可并行计算、sleep 可响应取消。详见附录问题 2（✅ 已修复）。
2. **estimated_tokens = prompt + 输出余量**：`_count_prompt_tokens(model_key, messages, max_tokens)` 返回 prompt tokens + `max_tokens`（输出上限的保守估算），TPM 桶按"请求可能消耗的最大 token"扣减。见附录问题 3（✅ 已修复）。
3. **`acquire` 返回值语义**：返回桶内等待时间（wait1+wait2），不含 `retry_after` 的 sleep（后者是独立的事前等待）。调用方通常忽略返回值。见附录问题 4（✅ 已修复）。
4. **配置 0 = 禁用限流**：`RateLimiterManager.get` 用 `getattr(settings, field, 0)`；`TokenBucket.acquire` 对 `refill_rate <= 0` 直接放行，`rpm/tpm` 配置为 0（或缺失）即无限流。见附录问题 1（✅ 已修复）。

---

## 工业级对比：修复方案 vs 主流实现

> 本节对照工业级限流实现（Go `x/time/rate`、Guava、Bucket4j、resilience4j、LiteLLM、aiolimiter、Fenic 等），检视我们针对附录 4 个缺陷的修复方案。调研日期 2026-08-02，来源见各节。

### 对比 1：「禁用限流」的表达（对应问题 1，配置 0 除零崩溃）

**工业级语义：`0` 不是「禁用」，而是「不允许任何事件」。** 禁用必须用显式开关或极大值表达。

| 库 | 0 的语义 | 禁用方式 |
| --- | --- | --- |
| Go `x/time/rate` | "A zero Limit allows no events"——桶不再补 token，消费完 burst 后全部拒绝 | `rate.Inf`（`Limit(math.MaxFloat64)`）允许所有事件 |
| Bucket4j（Java） | 无 0 特殊语义 | `replaceConfiguration(无限大配置, RESET)` 动态切换，或 starter 层 `enabled=false` |
| resilience4j | 无独立 enabled 布尔 | 参数配置为极大值 + 上层开关 |

> 真实事故：Traefik 曾依赖 `x/time/rate` 的 0-limit 行为实现「禁用」，2024 年 x/time 修复 0-limit 语义（`NewLimiter(0, b)` 会预填 burst token，首个请求仍通过）后被迫改用 `SetLimit(rate.Inf)`。[Traefik PR #9621](https://github.com/traefik/traefik/pull/9621)

**我们的方案 vs 工业级**：

- 我们采用「`refill_rate <= 0` 直接放行」——**语义上是把 0 当作禁用**。这与工业级「0 = 拒绝一切」相反，但**在本项目语境下是安全的**：我们的 `capacity` 与 `refill_rate` 同源于 `rpm/tpm` 配置，`0` 只会来自「配置未填」，不会来自「业务上要禁止某模型」——那是 `RateLimiterManager.get` 对未知 key 抛 `ValueError` 的职责。
- **差异提醒**：工业级更推荐「显式 `enabled` 开关」，语义更清晰、且不会产生「0 到底算禁用还是拒绝」的歧义。若未来需要表达「某个模型限流关闭但配置仍是 0」，应引入独立的开关字段，而非继续依赖 0 的特殊处理。

**来源**：[x/time/rate 文档](https://pkg.go.dev/golang.org/x/time/rate) · [x/time/rate 源码](https://go.googlesource.com/time/+/refs/tags/v0.10.0/rate/rate.go) · [Traefik PR #9621](https://github.com/traefik/traefik/pull/9621) · [Bucket4j 动态启用/禁用](https://stackoverflow.com/questions/76016472/bucket4j-enable-or-disable-dynamically/76036679#76036679)

### 对比 2：等待是否持锁（对应问题 2，持锁 sleep 阻塞其他请求）

**工业级共识：锁内只做记账（reserve），锁外等待。** 没有任何主流实现会在持锁状态下 sleep。

| 库 | 等待模型 | 持锁等待？ | 取消/超时 |
| --- | --- | --- | --- |
| Go `x/time/rate` | `reserveN` 锁内记账 → `Wait(ctx)` 锁外 timer/select | 否 | `ctx.Done()` 取消时 `Cancel()` 退还 token |
| Guava `acquire()` | `reserve()` 锁内记账 → `sleepMicrosUninterruptibly()` 锁外 sleep | 否（源码注释确认刻意设计） | 仅 `tryAcquire(timeout)` 有超时 |
| Bucket4j | 非阻塞 `tryConsume()`（拿不到返回 false），等待由调用方做 | 否（默认 LOCK_FREE/CAS） | 由调用方处理 |

**我们的方案 vs 工业级**：

- 我们的「锁内计算 → 锁外 sleep → 循环重检」**与 Guava 的 `reserve` + 锁外 sleep 同构**——锁只做记账，等待让出事件循环（asyncio 等价于 Go 的锁外等待）。方向正确。
- **差异提醒（可改进点）**：我们当前在 `acquire` 路径下**没有「取消时退还已预留 token」**的语义——等待中的 acquire 被取消即无事发生，桶不被污染（`test_bucket_cancel_does_not_corrupt_state` 已覆盖）。**已通过新增的 reserve/settle 形态补全**：`Reservation` 的 `settle(actual)` / `cancel()` 提供「先预留、后结算/退还」的完整语义（见「组件详解」reserve/settle 节与对比 3）。

**来源**：[x/time/rate 源码（reserveN/WaitN/CancelAt）](https://go.googlesource.com/time/+/refs/tags/v0.10.0/rate/rate.go) · [Guava RateLimiter 源码（acquire/reserve 两段式）](https://guava.dev/releases/17.0/api/docs/src-html/com/google/common/util/concurrent/RateLimiter.html#line.274) · [Bucket4j SynchronizationStrategy](https://javadoc.io/static/com.bucket4j/bucket4j_jdk8-core/8.10.1/io/github/bucket4j/local/SynchronizationStrategy.html)

### 对比 3：TPM 消耗估算（对应问题 3，只算 prompt 低估输出）

**工业级共识：TPM 官方口径 = 输入 + 输出都计入；单次调用预估值 = prompt + max_tokens（输出上限），请求完成后按实际 usage 结算。** 只算 prompt 是已知的严重低估。

| 方案 | 输入侧 | 输出侧 | 结算 |
| --- | --- | --- | --- |
| OpenAI/Azure 官方 | 提交时估算 | `estimated = prompt_tokens + max_tokens × best_of` | 响应 `usage` 事后校准 |
| LiteLLM | tiktoken 精确计数 | 预留有效输出上限（max_tokens → 模型上限 → 4096） | ITPM/OTPM 分桶，实际 `prompt+completion` 结算 |
| Fenic 网关 | tiktoken 精确计数 | p95 自适应预留（推理模型 p99）× 1.15 | 结算退差 `reserved − actual`（吞吐提升约 23×） |

**我们的方案 vs 工业级**：

- 我们采用「prompt（tiktoken 精确）+ `max_tokens` 输出余量」——**与 OpenAI 官方估算公式、LiteLLM 的预留上限一致**，方向正确。
- **差异提醒（可改进点，部分已落地）**：
  1. **结算退差 ✅ 已实现**（2026-08-02）：新增 `reservation_limiter.py` 的 `ReservationLimiter.reserve() → Reservation`（独立文件），请求完成后 `settle(actual)` 退 TPM 差（`max(0, est-actual)`），`llm_service` 已迁移到 reserve/settle 统一闭环。`max_tokens` 是上限、实际输出远小于它导致的 TPM 偏保守问题已缓解。详见 [reservation_limiter.md](reservation_limiter.md)。
  2. **`max_tokens` 设得过大仍会空耗配额（已缓解但未消除）**——结算退差在「实际消耗 > 预留」时无法补扣，`max_tokens` 若远大于实际输出，预留阶段仍按上限扣、只是结算时退回。**「宁多勿少」的保守取舍仍成立**；要更精确需引入 Fenic 式的自适应分布预留（p95 自适应 × 1.15），暂未实现。

**来源**：[OpenAI Rate limits 文档](https://developers.openai.com/api/docs/guides/rate-limits) · [LiteLLM ITPM/OTPM 限流](https://docs.litellm.ai/docs/proxy/io_token_rate_limits) · [Fenic 自适应 token 估算设计](https://github.com/typedef-ai/fenic/blob/main/specs/adaptive_token_estimation_design.md) · [LangChain rate_limiters（仅时间限制）](https://reference.langchain.com/python/langchain-core/rate_limiters)

### 对比 4：限流器 API 形态（对应问题 5，`async with` 语义误导）

**工业级结论：RPM 按请求计数的桶可用 `async with`；TPM 按量计费的桶应用显式 `reserve/settle` 方法。** `async with`（无参）在按量场景有先天局限——硬编码消耗 1，且无法表达「先预留、后结算」。

| 库 | 形态 | 按量消耗支持 |
| --- | --- | --- |
| aiolimiter | `async with limiter:`（= `acquire(1)`）+ `await limiter.acquire(amount)` / `limiter.limit(amount)` | 提供带参变体 |
| limits | 纯方法：`hit()` / `test()` / `get_window_stats()` | `hit()` 每次 1，无上下文管理器 |
| aiometer | 批量任务形态 `run_all(max_per_second=...)` | 不适合单次按量扣减 |
| Go `x/time/rate` | `Reserve()` 返回 Reservation → `DelayFrom()` / `Wait(ctx)` → `CancelAt(t)` | 预留-取消模型 |

**我们的方案 vs 工业级**：

- 我们**移除了 `__aenter__/__aexit__`**（无使用方），`await acquire()` 成为唯一用法——与 limits、PyrateLimiter 的方法调用形态一致，避免了 `async with` 对 TPM 桶的「消耗 1」误导。**方向正确且更简洁**（业界 aiolimiter 需要用 `limiter.limit(amount)` 绕开这个局限，我们直接没有这个陷阱）。
- **差异提醒（可改进点，已落地）**：我们保留了 `acquire`（拉模式，向后兼容），并**在独立文件 `reservation_limiter.py` 新增 `ReservationLimiter.reserve() → Reservation`**——类似 Go 的 `Reserve()` → `CancelAt()` 形态，支持「预留 → 结算 → 退还」（`settle(actual)` / `cancel()`）。`llm_service` 已迁移到 reserve/settle 统一闭环，`acquire` 仍可用于不关心退差的场景。

**来源**：[aiolimiter GitHub](https://github.com/mjpieters/aiolimiter) · [aiolimiter Discussion #181（按 payload 限流）](https://github.com/mjpieters/aiolimiter/discussions/181) · [limits 文档](https://limits.readthedocs.io/) · [aiometer GitHub](https://github.com/florimondmanca/aiometer)

### 速查表

| 我们的缺陷 | 工业级正确做法 | 我们的方案 | 参照实现 | 结论 |
| --- | --- | --- | --- | --- |
| 配置 0 除零崩溃 | 显式 `enabled` 开关或 `Inf`/极大值；0 = 拒绝一切 | `refill_rate <= 0` 直接放行 | x/time/rate `rate.Inf`、Bucket4j `enabled=false` | ✅ 本项目语境下安全；未来需独立开关则演进 |
| 持锁 sleep 阻塞其他请求 | 锁内记账、锁外等待；等待让出事件循环、支持取消 | 锁内计算 → 锁外 sleep → 循环重检 | Guava `acquire()` 两段式、x/time/rate `Wait(ctx)` | ✅ 与工业级同构 |
| 只算 prompt 低估输出 | 预留 = prompt + max_tokens，完成后按实际结算退差 | prompt + max_tokens 输出余量 + **reserve/settle 结算退差** | OpenAI 估算公式、LiteLLM、Fenic | ✅ 已实现结算退差；自适应预留未实现 |
| `async with` 语义误导 | TPM 桶用显式 reserve/settle；RPM 桶可 `async with` | 移除上下文管理器；`acquire` 保留 + **新增 reserve/settle** | aiolimiter `acquire(amount)`、limits `hit()`、Go Reservation | ✅ 更简洁；reserve/settle 已落地 |

---

## 附录：2026-08-01 代码审核记录

> 本次审核（初始版本）发现的遗留问题，先记录待办；修复状态逐条标注（✅ 已修复）。

### 问题 1（严重）：配置为 0 时除零崩溃 ✅

**位置**：`TokenBucket.acquire`（`refill_rate <= 0` 防御处）+ `RateLimiterManager.get`（`getattr(settings, field, 0)` 兜底处）

**触发**：RPM/TPM 配置为 0（或缺失）→ `TokenBucket(capacity=0, refill_rate=0)` → 桶空时 `wait_time = needed / 0` → `ZeroDivisionError`。

**已实测**：`rpm=0` 时 `acquire` 直接崩溃。而「配置 0 表示禁用限流」是用户最自然的表达。

**修复（2026-08-02）**：采用建议方案二——`TokenBucket.acquire` 开头对 `refill_rate <= 0` 防御，直接放行返回 `0.0`。`capacity` 与 `refill_rate` 同源（`rpm/tpm` 配置为 0 时两者均为 0），一个 guard 覆盖所有路径，`RateLimiterManager.get` 无需改动。新增测试 `test_bucket_zero_refill_disabled`（断言立即放行、无等待、不崩溃）。

### 问题 2（中）：持锁 sleep ✅

**位置**：`TokenBucket.acquire`（`while True` 循环内）

**影响**：等待期间锁被持有，其他排队请求无法并行计算等待时间；长 sleep 阻塞短等待请求；sleep 期间无法响应取消。

**实测边界**：单桶总耗时由 refill_rate 支配，10 并发各 0.1s 总 1.0s，未出现放大。属理论缺陷，非紧急。

**修复（2026-08-02）**：按改进方向实施——`TokenBucket.acquire` 重构为「锁内计算 → 锁外 sleep → 循环重检」：锁内仅计算 `wait_time`，`asyncio.sleep` 移到锁外，醒来回到循环顶部重新检查（sleep 期间 token 可能被其他请求抢走，重检保证公平且不过等）。等待期间锁不被持有，其他请求可并行计算；sleep 期间可响应取消（`CancelledError` 正常传播）。**连带解决问题 6**：新实现只在 `_tokens >= tokens` 时才扣减，不再出现负 token。新增测试 `test_bucket_wait_does_not_block_others`（短等待不被长等待阻塞）+ `test_bucket_cancel_does_not_corrupt_state`（取消不破坏桶状态）。

### 问题 3（中）：TPM 桶只算 prompt token ✅

**位置**：`_count_prompt_tokens`（llm_service.py）

**影响**：`estimated_tokens` 不含输出 token（completion），输出大时 TPM 桶低估实际消耗。

**修复（2026-08-02）**：采纳「加 max_tokens 余量」方案——`_count_prompt_tokens(model_key, messages, max_tokens)` 新增第三参，估算 = prompt tokens + `max_tokens`（输出上限的保守余量）。TPM 桶按"请求可能消耗的最大 token"扣减，宁可高估不错放。`async_generate()` / `generate()` 两处调用点传入各自的实际 `max_tokens`；签名向后兼容（默认 0 = 退化为旧口径）。

### 问题 4（低）：`acquire` 返回值表述不准确 ✅

**位置**：`RateLimiter.acquire` 的 docstring「总等待时间」

**问题**：实际返回 `wait1 + wait2`（桶内等待），**不含** `retry_after` 的 sleep。docstring 措辞「总等待时间」与实现不符；调用方集成点也忽略了返回值。

**修复（2026-08-02）**：采纳「docstring 改为桶内等待时间」——明确标注返回 `wait1 + wait2`、不含 `retry_after` 的 sleep，并提示调用方如需完整墙钟等待应自行计时。同步补充 `estimated_tokens` 的语义（RPM 桶固定扣 1、TPM 桶按此值扣）。

### 问题 5（低）：`async with` 用法误导 ✅

**位置**：`RateLimiter` 顶部 docstring + 原 `__aenter__`/`__aexit__`（已移除）

**问题**：docstring 主推 `async with limiter:`，但 `__aenter__` 调 `acquire()` 无参（estimated_tokens=0，TPM 桶退化）；`__aexit__` 空操作无释放语义。实际集成点都直接 `await acquire(estimated_tokens=...)`，docstring 与实际脱节。

**修复（2026-08-02）**：采用更彻底的方案——**移除 `__aenter__` / `__aexit__` 死代码**（已确认全项目无 `async with limiter:` 使用方，仅 docstring 与类自身）。`await acquire()` 成为唯一用法，模块 docstring 同步改为直接用法示例，消除「TPM 桶退化」的误导路径。

### 问题 6（低）：`_tokens` 可轻微为负 ✅

**位置**：`TokenBucket.acquire`（扣减 token 处）

**问题**：sleep 后直接 `-= tokens`，浮点时序可能产生负零点几。当前无害，可加 `max(0, ...)` 加固。

**修复（2026-08-02）**：由问题 2 的重构**连带解决**——新实现只在 `_tokens >= tokens` 的分支内扣减，等待中的请求在 sleep 后回到循环顶部重检（不足则继续等、不会扣），不存在"先扣为负"的路径。新增测试 `test_bucket_cancel_does_not_corrupt_state` 覆盖取消路径下的桶状态完整性。
