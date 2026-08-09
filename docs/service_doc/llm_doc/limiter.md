# Limiter 客户端限流设计文档

> **模块**：`app/services/llm/rate_limiter.py`（acquire 形态）+ `app/services/llm/reservation_limiter.py`（reserve/settle 形态）
> **职责**：LLM API 调用的客户端限流（RPM + TPM 双 Token Bucket）
> **两种形态**：`rate_limiter.py`（acquire：一次性扣减不退款，独立保留）与 `reservation_limiter.py`（reserve/settle：先预留后结算退差，`llm_service` 实际使用），独立实现、不共用代码，仅 API 形态不同
> **配套**：集成于 `LLMService.async_generate()` / `generate()`，见 `llm_service.py`

---

## 目录

- [Limiter 客户端限流设计文档](#limiter-客户端限流设计文档)
  - [目录](#目录)
  - [设计目标](#设计目标)
    - [LLM 限流：等待 vs 拒绝（2026-08-06 决策）](#llm-限流等待-vs-拒绝2026-08-06-决策)
  - [两种 API 形态：acquire vs reserve/settle](#两种-api-形态acquire-vs-reservesettle)
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
    - [RateLimiter — 双桶组合（acquire 形态）](#ratelimiter--双桶组合acquire-形态)
    - [RateLimiterManager — 实例管理](#ratelimitermanager--实例管理)
    - [Reservation — 预留对象（终态幂等）](#reservation--预留对象终态幂等)
    - [ReservationLimiter — 双桶组合（reserve/settle 形态）](#reservationlimiter--双桶组合reservesettle-形态)
    - [OutputTokenEstimator — 自适应输出估算器](#outputtokenestimator--自适应输出估算器)
    - [reserve_adaptive — 自适应预留](#reserve_adaptive--自适应预留)
    - [ReservationLimiterManager — 实例管理](#reservationlimitermanager--实例管理)
    - [其他限流算法组件（参考实现）](#其他限流算法组件参考实现)
  - [调用流程（reserve/settle 生产形态）](#调用流程reservesettle-生产形态)
  - [与重试/熔断的分层配合](#与重试熔断的分层配合)
  - [配置项清单](#配置项清单)
  - [已知边界与设计取舍](#已知边界与设计取舍)
  - [代码审核与工业级对比（问题 → 修复 → 工业对照）](#代码审核与工业级对比问题-修复-工业对照)
    - [问题 1（严重）：配置为 0 时除零崩溃 ✅](#问题-1严重配置为-0-时除零崩溃-)
      - [工业级对照：「禁用限流」的表达](#工业级对照禁用限流的表达)
    - [问题 2（中）：持锁 sleep ✅](#问题-2中持锁-sleep-)
      - [工业级对照：等待是否持锁](#工业级对照等待是否持锁)
    - [问题 3（中）：TPM 桶只算 prompt token ✅](#问题-3中tpm-桶只算-prompt-token-)
      - [工业级对照：TPM 消耗估算](#工业级对照tpm-消耗估算)
      - [对比 3.2：自适应预留（Fenic 式，2026-08-06 实现）](#对比-32自适应预留fenic-式2026-08-06-实现)
    - [问题 4（低）：`acquire` 返回值表述不准确 ✅](#问题-4低acquire-返回值表述不准确-)
    - [问题 5（低）：`async with` 用法误导 ✅](#问题-5低async-with-用法误导-)
      - [工业级对照：限流器 API 形态](#工业级对照限流器-api-形态)
    - [问题 6（低）：`_tokens` 可轻微为负 ✅](#问题-6低_tokens-可轻微为负-)
    - [速查表](#速查表)

---

## 设计目标

1. **保护配额**：在到达服务商硬限额前主动限流，避免 429 触发重试风暴
2. **允许突发**：Agent 场景下工具调用常集中在一小段时间，限流器要能处理瞬时尖峰
3. **按模型独立**：main / reasoning / fast 三套模型配额不同，各自独立记账
4. **透明等待**：限流是排队而非拒绝——请求等待配额，不失败，对上层无感

### LLM 限流：等待 vs 拒绝（2026-08-06 决策）

**结论：LLM 客户端限流应「等待」（排队），而非「拒绝」。**

**为什么拒绝不适合 LLM**：

| 维度 | LLM 调用 | 通用 Web API |
| --- | --- | --- |
| 失败成本 | 高（token 白烧、对话中断、用户等待） | 低（前端可重试） |
| 调用方 | 单个 Agent/用户（同一次对话的连续工具调用） | 大量并发用户 |
| 突发来源 | 一次 ReAct 循环里连续多次工具调用 | 用户流量高峰 |
| 语义 | 这次调用必须要完成才能推进对话 | 请求可有可无 |

LLM 场景下，**拒绝 = 一次工具调用失败 → Agent 循环中断 → 用户对话卡住**——不是「少一次请求」，而是「整条推理链断裂」。所以 LLM 客户端限流应该等待（排队），而非拒绝。

**与项目现状一致**：

- `TokenBucket.acquire` / `RateLimiter.acquire`：等待直到配额可用（等待型）
- `ReservationLimiter.reserve`：预留配额（等待），不是拒绝
- `retry.py` 对 429：退避等待重试（尊重 Retry-After），不放弃
- 设计目标第 4 条「透明等待」：限流是排队而非拒绝

**工业级对照**：通用漏桶的「桶满丢弃」语义是给「可丢弃流量」（日志、事件、非关键请求）设计的。LLM 调用是**不可丢弃的关键请求**，丢弃直接损害用户体验。工业 LLM 生态（OpenAI SDK、LiteLLM 等）的客户端限流均为等待型。

**对漏桶实现的含义**：`LeakyBucket` 在 LLM 语境下采用**等待型**（桶满排队等空位）而非标准漏桶的「满则丢弃」——输出仍恒定速率（保留平滑特性），但桶满时排队而非溢出，符合「透明等待」设计目标。

---

## 两种 API 形态：acquire vs reserve/settle

限流模块提供两种 API 形态，分别位于两个独立文件：

| 维度 | `rate_limiter.py`（acquire） | `reservation_limiter.py`（reserve/settle） |
| --- | --- | --- |
| 核心 API | `await limiter.acquire(est)` → 返回等待时间 | `await limiter.reserve(est)` → `res.settle(actual)` |
| 结算能力 | 无（一次性扣减，不退款） | ✅ 结算退差（settle）/ 全额退（cancel） |
| 适用场景 | 不关心退差的简单调用 | 需按实际 usage 退还未用 TPM 配额 |
| 生产使用者 | 无（保留类供对比/兼容） | ✅ `llm_service.py` |

**为何要结算退差**：TPM 桶按 `prompt + max_tokens` 预留，实际输出往往远小于 `max_tokens`，长期偏保守低估可用量。reserve/settle 在请求完成后把未用完的配额退还给桶。工业级参照：Go `x/time/rate` Reservation、LiteLLM/Fenic 预留-结算协议。

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

双桶组合：一次请求同时扣 **RPM 桶 1 个** + **TPM 桶 estimated_tokens 个**。任一个桶不足都等待，两者都过才放行。

### Retry-After 响应头

服务端返回 429 时带的 `Retry-After` 秒数，指示客户端多久后重试。本模块 `acquire(retry_after=...)` / `reserve(retry_after=...)` 会**先** sleep 该时长，再扣双桶——尊重服务端反馈的退避建议，优先于本地估算。

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
ReservationLimiterManager.get(model_key) ──→ 共享 ReservationLimiter 实例（缓存）
                                                    │
                                                    ▼
                                              ReservationLimiter（双桶）
                                            ┌────────────┬────────────┐
                                            ▼            ▼            │
                                      RPM 桶        TPM 桶    retry_after 等待
                                  capacity=rpm  capacity=tpm      （最先）
                                            │
                                            ▼
                                        Reservation（预留对象）
                                      settle(actual) / cancel()
```

**分层**：

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 管理 | `RateLimiterManager` / `ReservationLimiterManager` | model_key → 限流器实例的缓存映射 |
| 组合 | `RateLimiter` / `ReservationLimiter` | RPM + TPM 双桶 + Retry-After 编排 |
| 算法 | `TokenBucket` | 单桶 Token Bucket 算法本体 |
| 结算 | `Reservation` | 预留配额持有 + settle 退差 / cancel 全额退 |

---

## 组件详解

### TokenBucket — 单桶算法

`rate_limiter.py` 与 `reservation_limiter.py` 各自持有**独立的 TokenBucket 实现**，算法一致（锁外 sleep 循环重检、refill_rate<=0 防御）。差异在接口：acquire 形态只提供 `acquire()`；reserve/settle 形态额外提供 `refund()`（结算退差需要退还配额）。以下为完整版（含 refund）：

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

    async def refund(self, tokens=1.0) -> None:
        # reserve/settle 形态独有：退还 token（受 capacity 封顶，best-effort）
        async with self._lock:
            self._refill()
            self._tokens = min(self.capacity, self._tokens + tokens)

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now
```

**要点**：

- **时间源**：`time.monotonic()`（单调时钟），不受系统时间调整影响
- **惰性补充**：`_refill()` 只在 acquire 时调用，无需后台定时器，按经过时间换算补 token
- **容量封顶**：`min(capacity, ...)`，闲置期间不会无限积累 token，突发上限被约束；`refund` 退款也不突破 capacity（突发上限不被破坏）
- **互斥**：`asyncio.Lock` 保证桶状态一致性（并发请求同时扣 token 不会超扣）；等待在锁外进行，锁不阻塞其他请求、可响应取消
- **配置 0 语义**：`refill_rate <= 0` 直接放行（配置 0 = 禁用限流，见「已知边界」边界 4）

**返回语义**：`acquire` 返回**桶内累计等待时间**（秒），桶充足时立即返回 `0.0`。

### RateLimiter — 双桶组合（acquire 形态）

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
class RateLimiterManager:
    _instances: ClassVar[dict[str, RateLimiter]] = {}
    _configs: ClassVar[dict[str, RateLimiterConfig]] = {}

    @classmethod
    def register_config(cls, configs: dict[str, RateLimiterConfig]) -> None:
        # 注入按 model_key 的限流配置（AppState 读 settings 后调用），并 reset 重建
    @classmethod
    def get(cls, model_key="main") -> RateLimiter:
        # 缓存命中 → 返回；未命中 → 按注入配置懒创建 + 缓存（任意 key 均接受）
    @classmethod
    def reset(cls) -> None:
        cls._instances.clear()
```

**要点**：

- **共享实例**：同一 model_key 复用同一个 `RateLimiter`——**双桶必须跨请求记账**，每次 new 会重置桶、等于没限流
- **懒加载**：首次 `get()` 才创建，按 `register_config()` 注入的配置构建（未注入的 key 用默认值）
- **同步无竞态**：`get` 无 await，GIL 下天然原子，不会双实例
- **`reset()`**：配置变更或测试时清空缓存
- **配置注入（2026-08-10）**：子模块不直接依赖 settings——`AppState.initialize()` 读 settings 组装 `RateLimiterConfig` 后调 `register_config()` 注入；model_key 由外部传入，不内置白名单（对齐 ClientManager）

### Reservation — 预留对象（终态幂等）

**文件**：`reservation_limiter.py`

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
        # 终态标记在全部退款完成后置位（取消中断时保持未终态，外层可续退）
        # actual 非 None 且挂回调时 → 触发 settle_callback(actual)（喂估算器样本）
    async def cancel(self) -> None:
        # 所有桶全额退还（请求未确认发出时用）；终态标记在退款完成后置位
    @property
    def settled(self) -> bool: ...
```

**语义**：

- `settle(actual)`：请求完成后按实际消耗结算，对按量桶退 `max(0, reserved - actual)`；按次桶不退
- `settle(None)`：无 usage 时的保守路径——保留全部预留，但**标记终态**（闭环不泄漏）
- `cancel()`：请求未发出（create 失败/取消）时**所有桶全额退还**
- **终态幂等**：settle/cancel 任一调用后，再次调用为 no-op
- **取消泄漏防护（2026-08-09）**：终态标记 `_settled` 在**全部退款完成后**才置位——若退款循环中途被取消（`CancelledError` 向上传播），保持未终态，外层兜底（`llm_service` 的 `cancel`/`finally`）可续退剩余条目，避免部分桶配额永久泄漏。`TokenBucket.refund` 的 capacity 封顶保证重复退款不超发，兜底续退安全幂等

### ReservationLimiter — 双桶组合（reserve/settle 形态）

**文件**：`reservation_limiter.py`

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

**组合实现**：`reserve`（固定形态）与 `reserve_adaptive`（自适应形态）都委托 `_acquire`。核心逻辑：空构造 `Reservation()`，RPM 桶 `acquire(1.0)` 扣减后 `res.add(req_bucket, 1.0)` 追加为首条目（按次桶），TPM 桶 `acquire(est)` 扣减后 `res.add(token_bucket, est)` 追加为按量条目。组合 Reservation 的 `settle` 只命中非首条目、`cancel` 命中全部条目。**防 R5（组合两步间硬取消）**：TPM 预留被取消时，`except BaseException` 回退已扣的 RPM。

**组合 Reservation 语义（按「请求是否已发出」分界）**：

| 出口 | 方法 | 行为 |
| --- | --- | --- |
| create 失败/取消（请求未确认发出） | `cancel()` | 退 RPM 1 + TPM 全额 |
| create 成功后一切出口（整流/取消/成功） | `settle(actual)` | 退 TPM 差（`max(0, est-actual)`），RPM 不退 |

> **为何 settle 不退 RPM**：请求已真实发生，RPM 配额是真实消耗，退掉会让客户端以为自己有配额而服务端已超（触发 429）。cancel 只在「请求未确认发出」时退 RPM。

### OutputTokenEstimator — 自适应输出估算器

**文件**：`reservation_limiter.py`

**作用**：用「历史实际输出的高分位 × 安全系数」预测下一次预留的输出量，替代固定 `max_tokens` 上限——减少预留期间占桶（并发空耗）。Fenic 式设计，详见 [「对比 3.2」](#对比-32自适应预留fenic-式2026-08-06-实现)。

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

### reserve_adaptive — 自适应预留

`ReservationLimiter` 的自适应形态：

```python
res = await limiter.reserve_adaptive(prompt_tokens=100, max_tokens=4096)
# 预留量 = prompt + estimate()，clamp 到 prompt + max_tokens（只减不加）
# settle 时自动把 actual_total - prompt 喂给对应 max_tokens 的估算器池
```

- **结构性解耦**：provider 仍收宽裕 `max_tokens`（不截断输出），只有限流器预留下降
- **settle 回调**：`Reservation` 挂回调，`settle(actual)` 成功时喂样本；`settle(None)`/`cancel()` 不记录（无真实 usage 不污染分布）
- **按 max_tokens 分池**：`_estimators: dict[int, OutputTokenEstimator]`，不同输出上限独立建模
- **开关**：`llm_adaptive_reserve`（默认关），开启后 `llm_service` 走 `reserve_adaptive`，`reserve(estimated)` 保留兼容

### ReservationLimiterManager — 实例管理

**文件**：`reservation_limiter.py`

与 `RateLimiterManager` 同款缓存模式：`_instances: dict[str, ReservationLimiter]`，`get()` 懒创建 + 缓存复用，`reset()` 清空。配置由 `register_config()` 注入 `ReservationLimiterConfig`（按 model_key 各配 RPM/TPM/quantile 等），未注入的 key 用默认值。

**自适应预留分位数**：`ReservationLimiterConfig.quantile` 按 model_key 注入——`main`/`fast` 用 `llm_reserve_quantile`（p95），`reasoning` 用 `llm_reserve_reasoning_quantile`（p99，推理输出有相关性突发尖峰）。`get()` 构造 limiter 时从 config 取 `quantile` + 通用 `safety_margin`/`min_samples`/`window`。

### 其他限流算法组件（参考实现）

> 以下为 Token Bucket 之外的主流限流算法组件（`app/services/llm/rate_limiter.py`），接口与 `TokenBucket` 对齐（`acquire(tokens) -> float` 等待型 + `refund(tokens)` 退还），**未接入调用链**，供对比与按需选用。

#### LeakyBucket — 漏桶

**恒定速率流出**：请求入队，按固定速率出队执行；桶满拒绝。

**核心思想**：维护 `_next_ready`（下一次可放行时刻）。每次请求占 `tokens/refill_rate` 秒时间槽，`_next_ready` 向后推；请求到来时若未到 `_next_ready` 则等待。

**实现**：

```python
class LeakyBucket:
    """漏桶（Leaky Bucket）：恒定速率流出，请求入队排队。

    - capacity: 桶容量（最大排队请求数）
    - refill_rate: 每秒流出速率（出队速率，恒定）
    - _next_ready: 下一次可放行的时刻（单调时钟）
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._next_ready = 0.0  # 下一次可放行的时刻（单调时钟）
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        if self.refill_rate <= 0:
            return 0.0
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                # 桶满检查：等待中的请求数 × 每请求时间槽 ≥ 容量
                if self._next_ready > now and (
                    (self._next_ready - now) * self.refill_rate >= self.capacity
                ):
                    # 桶满：计算等第一个时间槽空出的时间（锁外 sleep）
                    wait_for_slot = max(
                        (self._next_ready - now) - (self.capacity / self.refill_rate),
                        0.0,
                    )
                else:
                    wait_for_slot = 0.0
                # 恒定速率：每 tokens 个请求占用 tokens/refill_rate 秒时间槽
                wait = max(self._next_ready - now, 0.0)
                if wait == 0.0 and wait_for_slot == 0.0:
                    # 无等待：锁内直接推进时间槽并返回
                    self._next_ready = (
                        max(now, self._next_ready) + tokens / self.refill_rate
                    )
                    return total_wait

            # 锁外 sleep：桶满等待 + 恒定速率等待，期间锁不被持有
            sleep_for = max(wait_for_slot, wait)
            await asyncio.sleep(sleep_for)
            total_wait += sleep_for
            # 回到循环顶部重检：sleep 期间可能被其他请求抢时间槽

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：回退时间槽（best-effort，不低于 now）。"""
        async with self._lock:
            self._next_ready = max(
                time.monotonic(), self._next_ready - tokens / self.refill_rate
            )
```

**与 Token Bucket 的区别**：

| 维度 | Token Bucket | 漏桶 |
| --- | --- | --- |
| 突发 | ✅ 允许（桶满可一次发 capacity 个） | ❌ 无（输出严格恒定） |
| 空闲期 | 攒 token，可应对高峰 | 攒不住，空闲期能力浪费 |
| 整形 | 长期平滑 | **严格整形**（恒定速率） |
| 适用 | Agent 突发调用 | 流量整形、视频流、队列背压 |

**可视化示例**（容量 8，恒定速率 2 QPS = 每 500ms 处理 1 个，全部同时到达模拟突发）：

```
1. 设定参数
   桶容量       8 个请求
   恒定流出速率  2 请求/秒 → 每 500ms 处理 1 个请求
   请求到达方式  全部同时到达（模拟突发流量）

2. 各请求等待时间（所有请求在 T=0ms 同时到达）
   请求      所在位置       预计处理时间点      等待时间
   Q1      桶内（队首）     T=0ms              0ms（桶空，立即处理）
   Q2      桶内            T=500ms            500ms
   Q3      桶内            T=1000ms           1000ms
   Q4      桶内            T=1500ms           1500ms
   Q5      桶内            T=2000ms           2000ms
   Q6      桶内            T=2500ms           2500ms
   Q7      桶内            T=3000ms           3000ms
   Q8      桶内（队尾）     T=3500ms           3500ms
   Q9      桶外等待        —                  —
   Q10     桶外等待        —                  —

3. 时间轴可视化
   请求到达时刻（T=0ms）：Q1 Q2 Q3 Q4 Q5 Q6 Q7 Q8  Q9 Q10
                                    ↓
              ┌──────────────────────────────────────┐
              │          漏桶（容量=8）               │
              │  Q8 Q7 Q6 Q5 Q4 Q3 Q2 Q1  →  →  →  →│  → 以 2个/秒 匀速流出
              └──────────────────────────────────────┘

   处理时间线：
   Q1  ──── 0ms  （立即处理）
   Q2  ──────── 500ms
   Q3  ──────────── 1000ms
   Q4  ──────────────── 1500ms
   Q5  ──────────────────── 2000ms
   Q6  ──────────────────────── 2500ms
   Q7  ──────────────────────────── 3000ms
   Q8  ──────────────────────────────── 3500ms

4. 关键结论
   - Q1~Q8 全部被接纳，但等待时间线性递增（间隔 = 500ms）
   - 输出绝对平滑：无论输入多猛，输出始终每 500ms 一个请求，后端无突发压力
   - 桶满（Q9/Q10）在标准漏桶中直接丢弃；**LLM 场景本实现为等待型变体**：
     Q9/Q10 排队等待空位而非丢弃（输出速率仍恒定，见「LLM 限流：等待 vs 拒绝」）
```

> **标准漏桶 vs LLM 等待变体**：标准漏桶桶满即丢（图 Q9/Q10 溢出）；本项目 LLM 场景用等待型变体——桶满排队而非丢弃，保留恒定输出速率，但请求不失败（见「LLM 限流：等待 vs 拒绝」决策）。

#### FixedWindowLimiter — 固定窗口

**按固定时间窗计数**：窗口内达到上限即拒绝/等待。

**实现**：

```python
class FixedWindowLimiter:
    """固定窗口（Fixed Window）：按固定时间窗计数，窗口内达到上限即拒绝。

    - rate: 每窗口最大请求数
    - window_seconds: 窗口长度（秒）
    - _window_start/_count: 当前窗口起始时刻 + 计数
    """

    def __init__(self, rate: float, window_seconds: float) -> None:
        self.rate = rate
        self.window_seconds = window_seconds
        self._window_start = 0.0
        self._count = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                if now - self._window_start >= self.window_seconds:
                    self._window_start = now
                    self._count = 0.0
                if self._count + tokens <= self.rate:
                    self._count += tokens
                    return total_wait
                # 窗口内已满：计算窗口翻转剩余时间，锁外 sleep
                wait = self.window_seconds - (now - self._window_start)

            # 锁外 sleep：等待窗口翻转，期间锁不被持有
            await asyncio.sleep(wait)
            total_wait += wait
            # 回到循环顶部重检：窗口已翻转则扣减放行

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：递减当前窗口计数（best-effort，不低于 0）。"""
        async with self._lock:
            self._count = max(0.0, self._count - tokens)
```

**核心思想**：窗口内 `_count + tokens ≤ rate` 放行；超限等待窗口翻转后重置计数。

**可视化示例**（rate=5 请求/窗口，window=60s，请求稀疏分布）：

```
1. 设定参数
   窗口长度      60 秒
   每窗口上限    5 个请求

2. 窗口计数（请求到达时刻）
   窗口 [0,60)：  R1 R2 R3 R4 R5    → 计数 5/5，满
   窗口 [60,120)： R6                → 窗口翻转，计数重置 1/5
   窗口 [120,180)： R7 R8            → 计数 2/5

   时间轴：
   0s        60s        120s       180s
   ┌──────────┐──────────┐──────────┐
   │ R1..R5=5 │ R6=1     │ R7 R8=2  │  ← 每窗口独立计数
   └──────────┘──────────┘──────────┘

3. 边界双倍问题（核心缺陷）
   59s 用满 5 个 → 61s 又用满 5 个 → 2 秒内实际发出 10 个（两倍配额）
   0s        59s 60s     61s                120s
   ┌─────────┐──────────┐───...─────┐
   │ 5 个满   │ 窗口切换  │ 5 个又满   │  ← 交界瞬间 10 个
   └─────────┘──────────┘───...─────┘
   R1 R2 R3 R4 R5   R6 R7 R8 R9 R10   → 实际 10 个/2秒，超配额 2 倍

4. 关键结论
   - 实现最简单：一个计数器 + 窗口边界判断，内存常数
   - 跨窗口交界无防护：窗口末尾用满 + 下窗口又用满 = 瞬间双倍
   - 适用：对瞬时峰值不敏感、实现优先的场景
```

#### SlidingWindowLogLimiter — 滑动窗口日志

**记录窗口内每次请求时间戳**，精确计数。

**实现**：

```python
class SlidingWindowLogLimiter:
    """滑动窗口日志：记录窗口内每次请求时间戳，精确计数。

    - rate: 窗口内最大请求数
    - window_seconds: 窗口长度（秒）
    - _timestamps: deque[float]，窗口内请求时间戳
    """

    def __init__(self, rate: float, window_seconds: float) -> None:
        self.rate = rate
        self.window_seconds = window_seconds
        self._timestamps: _deque[float] = _deque()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self.window_seconds
                while self._timestamps and self._timestamps[0] < cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) + tokens <= self.rate:
                    # 记录 tokens 个时间戳（近似：为按量支持，重复记录）
                    for _ in range(int(tokens)):
                        self._timestamps.append(now)
                    return total_wait
                # 窗口已满：计算最早时间戳过期剩余时间，锁外 sleep
                if self._timestamps:
                    wait = self._timestamps[0] + self.window_seconds - now
                else:
                    wait = 0.0

            # 锁外 sleep：等待最早时间戳过期，期间锁不被持有
            if wait > 0:
                await asyncio.sleep(wait)
                total_wait += wait
            # 回到循环顶部重检：过期时间戳已剔除则放行

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：移除最早的时间戳（best-effort）。"""
        async with self._lock:
            for _ in range(int(tokens)):
                if self._timestamps:
                    self._timestamps.popleft()
```

**核心思想**：`deque` 存时间戳，查询时剔除过期（`< now - window_seconds`），`len ≤ rate` 放行；满则等待最早时间戳过期。

**可视化示例**（rate=3 请求/窗口，window=60s）：

```
1. 设定参数
   窗口长度      60 秒
   窗口内上限    3 个请求

2. 时间戳记录与过期剔除
   R1 于 t=10s  记录 [10]
   R2 于 t=25s  记录 [10, 25]
   R3 于 t=40s  记录 [10, 25, 40]   ← 计数 3/3，满
   R4 于 t=50s  到达 → 窗口 [50-60, 50] 内已有 3 个 → 拒绝/等待

   t=50s 时的滑动窗口：窗口 = [t-60, t] = [-10, 50]
   ────────────[───│───│───│───]───
              10  25  40  (R4)
              已有 3 个 → 满

3. 滑动（随时间推移，旧时间戳过期）
   t=75s 时：窗口 = [15, 75]，R1@10 已过期剔除 → [25, 40]
   此时 R5 到达 → 计数 2/3 → 放行，记录 [25, 40, 75]

4. 关键结论
   - 最精确：任意时刻窗口内请求数真实反映，无边界双倍问题
   - 内存随窗口内请求量增长（每请求一个时间戳）——高 QPS 下开销大
   - 适用：低 QPS 但要求精确的场景
```

#### SlidingWindowCounterLimiter — 滑动窗口计数

**固定窗口 + 分桶加权折中**。

**实现**：

```python
class SlidingWindowCounterLimiter:
    """滑动窗口计数：固定窗口 + 分桶加权折中。

    - rate: 每窗口最大请求数
    - window_seconds: 窗口长度（秒）
    - buckets: 分桶数，窗口切成 N 个时间片
    - _counts: deque[(bucket_start, count)]
    """

    def __init__(
        self, rate: float, window_seconds: float, buckets: int = 4
    ) -> None:
        self.rate = rate
        self.window_seconds = window_seconds
        self.buckets = max(buckets, 1)
        self._bucket_size = window_seconds / self.buckets
        self._counts: _Deque[tuple[float, float]] = _deque()  # (bucket_start, count)
        self._lock = asyncio.Lock()

    def _current_bucket(self, now: float) -> tuple[float, float]:
        """返回 (当前桶起始时刻, 当前桶计数)；过期桶清理。"""
        cutoff = now - self.window_seconds
        while self._counts and self._counts[0][0] < cutoff:
            self._counts.popleft()
        current_start = now - (now % self._bucket_size)
        if self._counts and self._counts[-1][0] == current_start:
            return self._counts[-1]
        self._counts.append((current_start, 0.0))
        return self._counts[-1]

    def _window_count(self, now: float) -> float:
        """滑动窗口近似计数：当前桶 + 上一桶 × 剩余比例。"""
        current_start = now - (now % self._bucket_size)
        current = 0.0
        prev = 0.0
        for start, count in self._counts:
            if start == current_start:
                current = count
            else:
                prev += count
        elapsed_in_bucket = now - current_start
        weight = 1.0 - elapsed_in_bucket / self.window_seconds
        return current + prev * weight

    async def acquire(self, tokens: float = 1.0) -> float:
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._current_bucket(now)
                if self._window_count(now) + tokens <= self.rate:
                    self._counts[-1] = (self._counts[-1][0], self._counts[-1][1] + tokens)
                    return total_wait
                # 窗口已满：计算最老的桶滑出窗口剩余时间，锁外 sleep
                if self._counts:
                    oldest = self._counts[0][0]
                    wait = oldest + self.window_seconds - now
                else:
                    wait = 0.0

            # 锁外 sleep：等待最老的桶滑出窗口，期间锁不被持有
            if wait > 0:
                await asyncio.sleep(wait)
                total_wait += wait
            # 回到循环顶部重检：过期桶已清理则放行

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：递减当前桶计数（best-effort，不低于 0）。"""
        async with self._lock:
            now = time.monotonic()
            bucket = self._current_bucket(now)
            self._counts[-1] = (bucket[0], max(0.0, bucket[1] - tokens))
```

**核心思想**：窗口切成 N 个桶，滑动时按**剩余比例加权**上一桶计数。

**可视化示例**（window=60s, buckets=4 → 每桶 15s）：

```
1. 设定参数
   窗口长度      60 秒
   分桶数        4 桶 → 每桶 15s
   窗口上限      10 个请求

2. 分桶计数
   [0,15)  [15,30)  [30,45)  [45,60)      ← 4 个桶
     3        5        4        2          ← 各桶计数

3. 滑动窗口近似计算（t=50s 时）
   当前桶 [45,60) 已过 5s（50-45），剩余比例 = (60-50)/60 = 1/6
   近似窗口计数 = 当前桶 2 + 上一桶 4 × 1/6 ≈ 2.67
   若 ≤ 上限 10 → 放行

   t=50s 时刻：
   ┌───────┬───────┬───────┬──────┐
   │  3    │  5    │  4    │  2   │  ← 上一桶 × (60-50)/60
   └───────┴───────┴───────┴──────┘
                          ↑ 上一桶按剩余比例加权
                          近似计数 = 2 + 4×(1/6) ≈ 2.67

4. 关键结论
   - 内存常数（N 桶），比固定窗口平滑（无边界双倍），比滑窗日志省内存
   - 分桶粒度内不精确（边界内小幅突刺）
   - 适用：Redis 分桶（INCR + EXPIRE）、生产网关常见选择
```

#### GCRALimiter — GCRA（Generic Cell Rate Algorithm）

**以「理论到达时间 TAT」为核心的精确节流**，Token Bucket 的精确等价形式。

**实现**：

```python
class GCRALimiter:
    """GCRA（Generic Cell Rate Algorithm）：以「理论到达时间 TAT」为核心的精确节流。

    - rate: 每秒速率（个/秒）
    - burst: 突发容忍量（可连续放行的最大数）
    - _tat: 理论到达时间（单调时钟）
    """

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = rate
        self.burst = burst
        self._tat = 0.0  # 理论到达时间（单调时钟）
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        if self.rate <= 0:
            return 0.0
        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                # 等待时间 = max(0, 前一个 TAT - now)；首个请求 TAT=0 → 立即放行
                wait = max(self._tat - now, 0.0)
                if wait == 0.0:
                    # 无等待：锁内直接更新 TAT 并返回
                    self._tat = max(now, self._tat) + tokens / self.rate
                    return total_wait

            # 锁外 sleep：等待 TAT 到达，期间锁不被持有
            await asyncio.sleep(wait)
            total_wait += wait
            # 回到循环顶部重检：TAT 已到则更新并放行
            # （sleep 期间可能被其他请求推进 TAT）

    async def refund(self, tokens: float = 1.0) -> None:
        """退还配额：回退 TAT（best-effort，不低于 now）。"""
        async with self._lock:
            self._tat = max(time.monotonic(), self._tat - tokens / self.rate)
```

**核心思想**：

```
TAT = 上次请求的理论到达时间
新请求 → 等待 max(0, TAT - now)；TAT' = max(now, TAT) + 1/rate
  若 TAT' - now > burst/rate → 超出突发容忍（保守等待）
  否则 → 接受，TAT = TAT'
```

**可视化示例**（rate=2 请求/秒，burst=3，即间隔 500ms、可突发 3 个）：

```
1. 设定参数
   速率        2 请求/秒 → 每 500ms 一个
   突发容忍    3 个请求（可连续放行的最大数）

2. TAT 推演（首个请求 TAT 从 now 起步）
   请求     到达时刻       TAT 计算                TAT     等待
   R1      t=0ms         max(0, 0)+500 = 500      500ms   0ms（立即放行）
   R2      t=10ms        max(10,500)+500=1000     1000ms  490ms
   R3      t=20ms        max(20,1000)+500=1500    1500ms  980ms
   R4      t=30ms        max(30,1500)+500=2000    2000ms  1470ms

   时间轴（TAT 推进）：
   R1 ──── 0ms
   R2 ──────── ~500ms  （TAT 决定的下次放行点）
   R3 ──────────── ~1000ms
   R4 ──────────────── ~1500ms
   → 突发 3 个后，第 4 个起按 500ms 间隔节流

3. 突发 vs 速率
   - burst=3：允许连续 3 个快速到达（R1~R3 几乎同时排队放行）
   - 超过 burst 后：严格按 1/rate 间隔（R4 起每 500ms 一个）

4. 关键结论
   - 内存常数：只存一个 TAT（理论到达时间）
   - 精确节流：无边界双倍问题；单桶同时表达速率 + 突发上限
   - x/time/rate 的 advance 本质等价于 GCRA
```

**优点**：**内存常数**（只存一个 TAT）+ **精确节流**（无边界双倍）；单桶即可同时表达速率与突发上限。**缺点**：概念较抽象。**适用**：Ruby `rack/rate-limit`、部分 API Gateway；`x/time/rate` 的 `advance` 本质等价。

**接口统一说明**：以上 5 类组件接口与 `TokenBucket` 对齐（`acquire(tokens)` 返回等待秒数 + `refund(tokens)` 退还），可互换使用。**未接入 `llm_service` 调用链**——当前生产链路走 `reservation_limiter.py`（reserve/settle 形态）；`rate_limiter.py` 的 acquire 形态独立保留用于对比/兼容。

---

## 调用流程（reserve/settle 生产形态）

`llm_service.py` 集成的是 reserve/settle 形态（`ReservationLimiterManager`）：

```
async_generate() / generate()
    │
    ├─ limiter = ReservationLimiterManager.get(model_key)
    ├─ active: dict[str, Reservation] = {}
    │
    ├─ retry.execute(call_fn=_rate_limited_call)
    │     └─ 每次 call_fn（原始 + retry 内部重试）：
    │           if llm_adaptive_reserve:                     # 自适应形态（默认关）
    │               res = await limiter.reserve_adaptive(prompt_tokens, max_tokens)
    │           else:                                        # 固定形态（默认）
    │               res = await limiter.reserve(estimated_tokens=estimated)
    │           active["res"] = res
    │           try:  await create(**kwargs)
    │           except BaseException:  res.cancel(); active.pop; raise   # 失败全额退
    │     fallback（备用模型）：不参与 reserve，直接降级调用
    │
    ├─ 各出口闭环（按"请求是否已发出"分界）：
    │     ├─ 成功 / 整流 / 取消 → res.settle(usage 的 total 或 None)     # 退 TPM 差 + 喂估算器样本
    │     └─ 硬取消（finally 兜底）→ res.cancel()                        # 全额退
    └─ 整流重试 → 重新进入 retry.execute → 再次 reserve（自适应每轮重新评估）
```

**统一闭环**：每个 reserve 必配结算——create 失败 `cancel()` 全额退，create 成功后一切出口 `settle(actual)` 退 TPM 差；迭代硬取消由 `finally` 兜底，无 reservation 泄漏。

**关键点**：reserve 位于 call_fn 内部，**每次真实请求**（原始调用、retry 内部重试、整流重试）都重新 reserve。整流重试每轮重新进入 `retry.execute`，再次 reserve；测试已断言 `calls["acquire"] == 2`（整流 2 轮）。fallback 不参与 reserve（备用模型防突发无意义，独立于主模型配额）。

**acquire 形态流程**（对比/兼容用，未接入生产）：

```
async_generate() / generate()
    │
    ├─ estimated = _count_prompt_tokens(model_key, messages, max_tokens)  # 循环外一次
    ├─ limiter = RateLimiterManager.get(model_key)
    │
    ├─ retry.execute(call_fn=_rate_limited_call)
    │     └─ 每次 call_fn：
    │           await limiter.acquire(estimated_tokens=estimated)   # 一次性扣减，无结算
    │           await client.chat.completions.create(**kwargs)
    └─ 无结算步骤（acquire 不退款）
```

---

## 与重试/熔断的分层配合

```
 reserve()       create()       429 时 retry 内部
客户端预限流      正式请求      服务端反馈限流
```

- **限流（reserve/acquire）**：事前本地排队，配额不足时等待而非请求
- **重试/熔断（retry.execute）**：事后处理下游故障；对 429 尊重 `Retry-After` 退避
- **两者互补**：客户端限流减少触发服务端 429；真遇到 429，重试层兜底

**注意**：reserve/acquire 的 `retry_after` 与 retry 层的 `Retry-After` 是两条独立路径——前者是调用方显式传入的等待，后者是 429 异常响应头内提取的退避。

**跨模块问题**：限流与重试的配合（重试/降级是否计入限流申请）详见 [llm.md「配额缺口」章节](llm.md#配额缺口重试降级不计入限流申请)。

---

## 配置项清单

以下配置集中在 `app/config/settings.py`，由 `AppState.initialize()` 读 settings 组装 `RateLimiterConfig`/`ReservationLimiterConfig` 后经 `register_config()` 注入（子模块不直接依赖 settings）：

### RPM / TPM（双 Token Bucket）

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `llm_main_rpm` | int | `60` | 主模型每分钟请求数 |
| `llm_reasoning_rpm` | int | `30` | 推理模型每分钟请求数 |
| `llm_fast_rpm` | int | `100` | 快速模型每分钟请求数 |
| `llm_main_tpm` | int | `2000000` | 主模型每分钟 Token 消耗量 |
| `llm_reasoning_tpm` | int | `2000000` | 推理模型每分钟 Token 消耗量 |
| `llm_fast_tpm` | int | `2000000` | 快速模型每分钟 Token 消耗量 |

> TPM 默认值参考 DeepSeek 官方限额（2M tokens/分钟）。RPM/TPM 均需配合 Manager 使用。

### 自适应预留（Fenic 式，开关默认关）

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `llm_adaptive_reserve` | bool | `false` | 自适应预留开关（开启用高分位估算输出，减少占桶） |
| `llm_reserve_quantile` | float | `0.95` | 普通模型输出分位数（p95） |
| `llm_reserve_reasoning_quantile` | float | `0.99` | 推理模型分位数（p99，推理输出有相关性突发尖峰） |
| `llm_reserve_safety_margin` | float | `1.15` | 安全系数（1.0~4.0，越高越保守） |
| `llm_reserve_min_samples` | int | `30` | 冷启动阈值（样本不足回退静态上限） |
| `llm_reserve_window` | int | `256` | 滚动样本窗口（deque 上限） |

---

## 已知边界与设计取舍

1. **等待期间锁外 sleep**（`TokenBucket.acquire` 的 `while True` 循环）：「锁内计算 → 锁外 sleep → 循环重检」，等待期间锁不被持有，其他请求可并行计算、sleep 可响应取消。详见下文问题 2（✅ 已修复）。
2. **estimated_tokens = prompt + 输出余量**：`_count_prompt_tokens(model_key, messages, max_tokens)` 返回 prompt tokens + `max_tokens`（输出上限的保守估算），TPM 桶按"请求可能消耗的最大 token"扣减。见下文问题 3（✅ 已修复）；自适应预留进一步用高分位估算替代静态上限（见对比 3.2）。
3. **`acquire` 返回值语义**：返回桶内等待时间（wait1+wait2），不含 `retry_after` 的 sleep（后者是独立的事前等待）。调用方通常忽略返回值。见下文问题 4（✅ 已修复）。`reserve` 形态无返回值（返回 Reservation，等待发生在内部）。
4. **配置 0 = 禁用限流**：`register_config()` 注入的 `rpm`/`tpm` 为 0（或未注入该 key 用默认）；`TokenBucket.acquire` 对 `refill_rate <= 0` 直接放行，`rpm/tpm` 配置为 0（或缺失）即无限流。见下文问题 1（✅ 已修复）。
5. **reserve/settle 的终态幂等**：`Reservation.settle`/`cancel` 任一调用后再次调用为 no-op，防止重复结算；`settle(None)` 保留全部预留但标记终态，闭环不泄漏。
6. **防 R5（组合两步间硬取消）**：`reserve` 先扣 RPM 再扣 TPM，若 TPM 预留前被硬取消（`CancelledError`），`except BaseException` 回退已扣的 RPM。

---

## 代码审核与工业级对比（问题 → 修复 → 工业对照）

> 本节以 2026-08-01 代码审核发现的 6 个问题为主线，逐条记录**问题 → 修复 → 工业级对照**（对照 Go `x/time/rate`、Guava、Bucket4j、resilience4j、LiteLLM、aiolimiter、Fenic 等主流实现，调研日期 2026-08-02，来源见各节）。修复状态逐条标注（✅ 已修复）。完整速览见文末[速查表](#速查表)。

### 问题 1（严重）：配置为 0 时除零崩溃 ✅

**位置**：`TokenBucket.acquire`（`refill_rate <= 0` 防御处）+ `RateLimiterManager._build_for`（未注入配置时默认值兜底处）

**触发**：RPM/TPM 配置为 0（或缺失）→ `TokenBucket(capacity=0, refill_rate=0)` → 桶空时 `wait_time = needed / 0` → `ZeroDivisionError`。

**已实测**：`rpm=0` 时 `acquire` 直接崩溃。而「配置 0 表示禁用限流」是用户最自然的表达。

**修复（2026-08-02）**：采用建议方案二——`TokenBucket.acquire` 开头对 `refill_rate <= 0` 防御，直接放行返回 `0.0`。`capacity` 与 `refill_rate` 同源（`rpm/tpm` 配置为 0 时两者均为 0），一个 guard 覆盖所有路径，`RateLimiterManager.get` 无需改动。新增测试 `test_bucket_zero_refill_disabled`（断言立即放行、无等待、不崩溃）。

#### 工业级对照：「禁用限流」的表达

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

### 问题 2（中）：持锁 sleep ✅

**位置**：`TokenBucket.acquire`（`while True` 循环内）

**影响**：等待期间锁被持有，其他排队请求无法并行计算等待时间；长 sleep 阻塞短等待请求；sleep 期间无法响应取消。

**实测边界**：单桶总耗时由 refill_rate 支配，10 并发各 0.1s 总 1.0s，未出现放大。属理论缺陷，非紧急。

**修复（2026-08-02）**：按改进方向实施——`TokenBucket.acquire` 重构为「锁内计算 → 锁外 sleep → 循环重检」：锁内仅计算 `wait_time`，`asyncio.sleep` 移到锁外，醒来回到循环顶部重新检查（sleep 期间 token 可能被其他请求抢走，重检保证公平且不过等）。等待期间锁不被持有，其他请求可并行计算；sleep 期间可响应取消（`CancelledError` 正常传播）。**连带解决问题 6**：新实现只在 `_tokens >= tokens` 时才扣减，不再出现负 token。新增测试 `test_bucket_wait_does_not_block_others`（短等待不被长等待阻塞）+ `test_bucket_cancel_does_not_corrupt_state`（取消不破坏桶状态）。

#### 工业级对照：等待是否持锁

**工业级共识：锁内只做记账（reserve），锁外等待。** 没有任何主流实现会在持锁状态下 sleep。

| 库 | 等待模型 | 持锁等待？ | 取消/超时 |
| --- | --- | --- | --- |
| Go `x/time/rate` | `reserveN` 锁内记账 → `Wait(ctx)` 锁外 timer/select | 否 | `ctx.Done()` 取消时 `Cancel()` 退还 token |
| Guava `acquire()` | `reserve()` 锁内记账 → `sleepMicrosUninterruptibly()` 锁外 sleep | 否（源码注释确认刻意设计） | 仅 `tryAcquire(timeout)` 有超时 |
| Bucket4j | 非阻塞 `tryConsume()`（拿不到返回 false），等待由调用方做 | 否（默认 LOCK_FREE/CAS） | 由调用方处理 |

**我们的方案 vs 工业级**：

- 我们的「锁内计算 → 锁外 sleep → 循环重检」**与 Guava 的 `reserve` + 锁外 sleep 同构**——锁只做记账，等待让出事件循环（asyncio 等价于 Go 的锁外等待）。方向正确。
- **差异提醒（可改进点）**：我们当前在 `acquire` 路径下**没有「取消时退还已预留 token」**的语义——等待中的 acquire 被取消即无事发生，桶不被污染（`test_bucket_cancel_does_not_corrupt_state` 已覆盖）。**已通过新增的 reserve/settle 形态补全**：`Reservation` 的 `settle(actual)` / `cancel()` 提供「先预留、后结算/退还」的完整语义（见「组件详解」Reservation 节）。

**来源**：[x/time/rate 源码（reserveN/WaitN/CancelAt）](https://go.googlesource.com/time/+/refs/tags/v0.10.0/rate/rate.go) · [Guava RateLimiter 源码（acquire/reserve 两段式）](https://guava.dev/releases/17.0/api/docs/src-html/com/google/common/util/concurrent/RateLimiter.html#line.274) · [Bucket4j SynchronizationStrategy](https://javadoc.io/static/com.bucket4j/bucket4j_jdk8-core/8.10.1/io/github/bucket4j/local/SynchronizationStrategy.html)

### 问题 3（中）：TPM 桶只算 prompt token ✅

**位置**：`_count_prompt_tokens`（llm_service.py）

**影响**：`estimated_tokens` 不含输出 token（completion），输出大时 TPM 桶低估实际消耗。

**修复（2026-08-02）**：采纳「加 max_tokens 余量」方案——`_count_prompt_tokens(model_key, messages, max_tokens)` 新增第三参，估算 = prompt tokens + `max_tokens`（输出上限的保守余量）。TPM 桶按"请求可能消耗的最大 token"扣减，宁可高估不错放。`async_generate()` / `generate()` 两处调用点传入各自的实际 `max_tokens`；签名向后兼容（默认 0 = 退化为旧口径）。

#### 工业级对照：TPM 消耗估算

**工业级共识：TPM 官方口径 = 输入 + 输出都计入；单次调用预估值 = prompt + max_tokens（输出上限），请求完成后按实际 usage 结算。** 只算 prompt 是已知的严重低估。

| 方案 | 输入侧 | 输出侧 | 结算 |
| --- | --- | --- | --- |
| OpenAI/Azure 官方 | 提交时估算 | `estimated = prompt_tokens + max_tokens × best_of` | 响应 `usage` 事后校准 |
| LiteLLM | tiktoken 精确计数 | 预留有效输出上限（max_tokens → 模型上限 → 4096） | ITPM/OTPM 分桶，实际 `prompt+completion` 结算 |
| Fenic 网关 | tiktoken 精确计数 | p95 自适应预留（推理模型 p99）× 1.15 | 结算退差 `reserved − actual`（吞吐提升约 23×） |

**我们的方案 vs 工业级**：

- 我们采用「prompt（tiktoken 精确）+ `max_tokens` 输出余量」——**与 OpenAI 官方估算公式、LiteLLM 的预留上限一致**，方向正确。
- **差异提醒（可改进点，部分已落地）**：
  1. **结算退差 ✅ 已实现**（2026-08-02）：`ReservationLimiter.reserve() → Reservation`，请求完成后 `settle(actual)` 退 TPM 差（`max(0, est-actual)`），`llm_service` 已迁移到 reserve/settle 统一闭环。`max_tokens` 是上限、实际输出远小于它导致的 TPM 偏保守问题已缓解。详见「组件详解」Reservation 节。
  2. **`max_tokens` 设得过大仍会空耗配额（已缓解但未消除）**——结算退差在「实际消耗 > 预留」时无法补扣，`max_tokens` 若远大于实际输出，预留阶段仍按上限扣、只是结算时退回。**「宁多勿少」的保守取舍仍成立**；要更精确需引入 Fenic 式的自适应分布预留（p95 自适应 × 1.15）。**已实现（2026-08-06）**：见下文 [对比 3.2](#对比-32自适应预留fenic-式2026-08-06-实现)。

##### 「已缓解但未消除」的具体情形

**预留公式**（[llm_service.py](app/services/llm_service.py) 的 `_count_prompt_tokens`）：`estimated = prompt_tokens + max_tokens`（输出上限的保守估算），TPM 桶按此扣减。**这个数是「上限」而非「实际」**——实际输出往往远小于 max_tokens。

**「已缓解」——退差解决了什么**（单请求长期不耗空桶）：

```
预留 = prompt + max_tokens = 100 + 4096 = 4196 tokens  ← 按上限扣
实际 = prompt + 实际输出  = 100 + 200   = 300 tokens
退差 = 4196 - 300 = 3896 tokens 退回桶
```

单个请求视角：扣 4196、退回 3896、最终实际只占 300。长期看桶不会被「高估」慢慢耗空——这就是「缓解」。

**「未消除」——问题出在「预留期间」**（退差发生在请求完成后）：

```
假设 TPM 桶容量 = 20,000，max_tokens = 4096，实际输出大多 200 tokens：

时刻       事件                         桶剩余
t0         请求 A 预留 4196             15,804
t0+ε       请求 B 预留 4196             11,608
t0+2ε      请求 C 预留 4196             7,412
t0+3ε      请求 D 预留 4196             3,216
t0+4ε      请求 E 预留 4196 → 不足！等待   0
```

**请求 E 实际只需 300 tokens**，但桶被 A~D 的「高估预留」占满，必须等 A~D 完成并退差后才能放行。**这就是「空耗」**：桶的「真实消耗」只有 4×300 = 1200，却因 4×4196 = 16,784 的高估预留被占住，第 5 个只需 300 tokens 的请求被不必要阻塞。退差只能事后释放，无法避免预留期间的占桶。

| 维度 | 退差能解决 | 退差解决不了 |
| --- | --- | --- |
| 时间 | 请求**完成后**释放 | 请求**进行中**的占桶 |
| 并发 | 单请求长期不耗空桶 | 多个高估预留**瞬时占满桶**，阻塞后续请求 |
| 方向 | 只退（actual < reserved） | 无法补扣（actual > reserved，超额部分无客户端限流保护） |

**一句话**：退差是「事后补救」，但「预留期间按上限占桶」导致的瞬时并发阻塞，退差来不及救。

> **这是设计取舍，非遗留待办**：「实际消耗 > 预留」无法补扣是「预留-结算」模型的结构性限制（超额部分本就不应靠客户端限流兜底）。「宁多勿少」的保守取舍是有意为之——配合对比 3.2 的自适应预留（已实现，开关默认关），空耗已最大程度缓解。此处记录为已知边界，不作为待实现功能。

**根治方向（Fenic 自适应预留）**：不用固定 `max_tokens` 预留，而是统计历史实际输出的分布，取 p95 值（推理模型 p99）× 1.15 作为预留量：

```
预留 = prompt + p95(历史实际输出) × 1.15
```

- 预留量贴近「真实可能消耗」而非「理论上限」
- 预留期间占桶大幅减少 → 并发空耗阻塞显著缓解
- 吞吐提升约 23×（Fenic 实测）

**来源**：[OpenAI Rate limits 文档](https://developers.openai.com/api/docs/guides/rate-limits) · [LiteLLM ITPM/OTPM 限流](https://docs.litellm.ai/docs/proxy/io_token_rate_limits) · [Fenic 自适应 token 估算设计](https://github.com/typedef-ai/fenic/blob/main/specs/adaptive_token_estimation_design.md) · [LangChain rate_limiters（仅时间限制）](https://reference.langchain.com/python/langchain-core/rate_limiters)

#### 对比 3.2：自适应预留（Fenic 式，2026-08-06 实现）

**问题延续上文**：`max_tokens` 设得过大仍会空耗配额——预留期间按上限占桶导致并发空耗（多个高估预留瞬时占满 TPM 桶，阻塞只需少量 token 的请求），退差只能事后释放。**根治方向 = 自适应预留**：用「历史实际输出的高分位 × 安全系数」替代固定 `max_tokens` 预留。

**工业级实现对比**（调研 2026-08-06）：

| 实现 | 预测方式 | 分位数 | 键控 | 特殊点 |
| --- | --- | --- | --- | --- |
| **Fenic** | 滚动分布（deque 上限 256） | p95 普通 / **p99 推理** | (profile, max_tokens) | 安全系数 1.15、min_samples 30、clamp 只减不加、结构性解耦 |
| **Valyu** | 滚动分布 + EMA 误差修正 | **P80 经验最优** | (任务模式, 阶段, 模型) | 阶段边界是难点；P50 欠预留，P95 省不了多少 |
| **Qwen-code** | 非分位：「低默认 + 截断升级」 | — | 模型 | 8K 默认，截断则升到模型上限；<1% 请求截断，平均槽位预留降 ~4x |
| **liteLLM** | pre-call 预留 input + output cap | — | 模型 | ITPM/OTPM 分桶，post-call 结算 |

**工业共识（三层结构）**：

1. **结算（settle）是确定性基础**——每次响应后 `reserved − actual` 退差，可靠回收吞吐
2. **自适应估算是机会加速器**——用「滚动分布高分位 × 安全系数」缩小预留量，减少在途请求占桶
3. **clamp 到静态上限**——预留量只减不加（≤ `prompt + max_tokens`），429 风险有界

**我们的选型**（已实现，开关 `llm_adaptive_reserve` 默认关）：

- **`OutputTokenEstimator`**（`reservation_limiter.py`）：滚动样本 deque（上限 256），分位数排序取索引（nearest-rank，无 numpy），普通模型 p95、推理模型 p99（`_QUANTILE_FIELD_BY_KEY` 按 model_key 区分）
- **安全系数** `llm_reserve_safety_margin`（默认 1.15，校验 [1.0, 4.0]）
- **冷启动回退**：样本 < `llm_reserve_min_samples`（默认 30）返回 0 → 调用方回退静态 `max_tokens`
- **clamp 只减不加**：`min(estimate, max_tokens)`
- **结构性解耦**：`llm_service` 的 provider `max_tokens` 保持宽裕不截断（`_build_chat_kwargs` 不动），只有限流器预留下降
- **settle 回调喂样本**：`Reservation` 挂回调，`settle(actual)` 成功时把 `actual_total − prompt` 喂给对应 max_tokens 的估算器池；`settle(None)`/`cancel()` 不记录（无真实 usage 不污染分布）
- **按 max_tokens 分池**：`_estimators: dict[int, OutputTokenEstimator]`，不同输出上限独立建模
- **429 风险兜底**：高分位 × 安全系数 + clamp + 现有 retry 退避/重试

**配置项**（`app/config/settings.py`）：`llm_adaptive_reserve`(False) / `llm_reserve_quantile`(0.95) / `llm_reserve_reasoning_quantile`(0.99) / `llm_reserve_safety_margin`(1.15) / `llm_reserve_min_samples`(30) / `llm_reserve_window`(256)。

> **启用方式**：`.env` 设 `LLM_ADAPTIVE_RESERVE=true`。开关为进程级静态（与 RPM/TPM 配置一致，运行时不可翻转）。开启后经 `reserve_adaptive(prompt_tokens, max_tokens)` 走自适应路径，`reserve(estimated)` 保留兼容。

### 问题 4（低）：`acquire` 返回值表述不准确 ✅

**位置**：`RateLimiter.acquire` 的 docstring「总等待时间」

**问题**：实际返回 `wait1 + wait2`（桶内等待），**不含** `retry_after` 的 sleep。docstring 措辞「总等待时间」与实现不符；调用方集成点也忽略了返回值。

**修复（2026-08-02）**：采纳「docstring 改为桶内等待时间」——明确标注返回 `wait1 + wait2`、不含 `retry_after` 的 sleep，并提示调用方如需完整墙钟等待应自行计时。同步补充 `estimated_tokens` 的语义（RPM 桶固定扣 1、TPM 桶按此值扣）。

### 问题 5（低）：`async with` 用法误导 ✅

**位置**：`RateLimiter` 顶部 docstring + 原 `__aenter__`/`__aexit__`（已移除）

**问题**：docstring 主推 `async with limiter:`，但 `__aenter__` 调 `acquire()` 无参（estimated_tokens=0，TPM 桶退化）；`__aexit__` 空操作无释放语义。实际集成点都直接 `await acquire(estimated_tokens=...)`，docstring 与实际脱节。

**修复（2026-08-02）**：采用更彻底的方案——**移除 `__aenter__` / `__aexit__` 死代码**（已确认全项目无 `async with limiter:` 使用方，仅 docstring 与类自身）。`await acquire()` 成为唯一用法，模块 docstring 同步改为直接用法示例，消除「TPM 桶退化」的误导路径。

#### 工业级对照：限流器 API 形态

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

### 问题 6（低）：`_tokens` 可轻微为负 ✅

**位置**：`TokenBucket.acquire`（扣减 token 处）

**问题**：sleep 后直接 `-= tokens`，浮点时序可能产生负零点几。当前无害，可加 `max(0, ...)` 加固。

**修复（2026-08-02）**：由问题 2 的重构**连带解决**——新实现只在 `_tokens >= tokens` 的分支内扣减，等待中的请求在 sleep 后回到循环顶部重检（不足则继续等、不会扣），不存在"先扣为负"的路径。新增测试 `test_bucket_cancel_does_not_corrupt_state` 覆盖取消路径下的桶状态完整性。

### 速查表

| 我们的缺陷 | 工业级正确做法 | 我们的方案 | 参照实现 | 结论 |
| --- | --- | --- | --- | --- |
| 配置 0 除零崩溃 | 显式 `enabled` 开关或 `Inf`/极大值；0 = 拒绝一切 | `refill_rate <= 0` 直接放行 | x/time/rate `rate.Inf`、Bucket4j `enabled=false` | ✅ 本项目语境下安全；未来需独立开关则演进 |
| 持锁 sleep 阻塞其他请求 | 锁内记账、锁外等待；等待让出事件循环、支持取消 | 锁内计算 → 锁外 sleep → 循环重检 | Guava `acquire()` 两段式、x/time/rate `Wait(ctx)` | ✅ 与工业级同构 |
| 只算 prompt 低估输出 | 预留 = prompt + max_tokens，完成后按实际结算退差 | prompt + max_tokens 输出余量 + **reserve/settle 结算退差** + **自适应预留（高分位×安全系数）** | OpenAI 估算公式、LiteLLM、Fenic | ✅ 已实现结算退差 + 自适应预留（开关默认关，见对比 3.2） |
| `async with` 语义误导 | TPM 桶用显式 reserve/settle；RPM 桶可 `async with` | 移除上下文管理器；`acquire` 保留 + **新增 reserve/settle** | aiolimiter `acquire(amount)`、limits `hit()`、Go Reservation | ✅ 更简洁；reserve/settle 已落地 |
