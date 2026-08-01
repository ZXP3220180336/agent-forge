# RateLimiter 设计文档

> **模块**：`app/services/llm/rate_limiter.py`
> **职责**：LLM API 调用的客户端限流（RPM + TPM 双 Token Bucket）
> **配套**：集成于 `LLMService.async_generate()` / `generate()`，见 `llm_service.py`

---

## 目录

- [设计目标](#设计目标)
- [核心概念解释](#核心概念解释)
  - [限流（Rate Limiting）](#限流rate-limiting)
  - [Token Bucket 算法](#token-bucket-算法)
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
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            needed = tokens - self._tokens
            wait_time = needed / self.refill_rate
            await asyncio.sleep(wait_time)   # 持锁等待（见「已知边界」）
            self._refill()
            self._tokens -= tokens
            return wait_time

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
- **互斥**：`asyncio.Lock` 保证桶状态一致性（并发请求同时扣 token 不会超扣）

**返回语义**：`acquire` 返回**桶内等待时间**（秒），桶充足时立即返回 `0.0`。

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
- **TPM 桶按 `estimated_tokens` 扣**：请求前预估的 prompt token 数（`max(..., 1.0)` 防止 0 造成桶不扣）
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

- **共享实例**：同一 model_key 复用同一个 RateLimiter——**双桶必须跨请求记账**，每次 new 会重置桶、等于没限流
- **懒加载**：首次 `get()` 才创建，读取 `settings` 的 RPM/TPM 配置
- **同步无竞态**：`get` 无 await，GIL 下天然原子，不会双实例
- **`reset()`**：配置变更或测试时清空缓存

---

## 调用流程

```
async_generate() / generate()
    │
    ├─ estimated = _count_prompt_tokens(model_key, messages)   # tiktoken 实时数（循环外一次）
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

1. **等待期间持锁**（[TokenBucket.acquire](app/services/llm/rate_limiter.py#L50-L63)）：等待补充时持有 `asyncio.Lock`，阻塞其他排队请求并行计算。单桶场景总耗时由 refill_rate 支配，实测 10 并发各 0.1s 总 1.0s 符合预期；但长等待会阻塞短等待请求，且无法在 sleep 中响应取消。详见附录问题 2。
2. **estimated_tokens 仅含 prompt**：`_count_prompt_tokens` 只数 messages，不含输出 token。TPM 桶可能低估实际消耗（输出大时偏差明显）。见附录问题 3。
3. **`acquire` 返回值语义**：返回桶内等待时间（wait1+wait2），不含 `retry_after` 的 sleep。调用方通常忽略返回值。见附录问题 4。
4. **配置 0 的语义未定义**：`RateLimiterManager.get` 用 `getattr(settings, field, 0)`，0 值会构造 capacity=0 的桶，桶空时除零崩溃。**配置 0 = 禁用限流**是最自然的表达，当前实现不满足。见附录问题 1。

---

## 附录：2026-08-01 代码审核记录

> 本次审核（初始版本）发现的遗留问题，尚未修复，先记录待办。

### 问题 1（严重）：配置为 0 时除零崩溃

**位置**：[rate_limiter.py:59](app/services/llm/rate_limiter.py#L59) + [rate_limiter.py:167-168](app/services/llm/rate_limiter.py#L167-L168)

**触发**：RPM/TPM 配置为 0（或缺失）→ `TokenBucket(capacity=0, refill_rate=0)` → 桶空时 `wait_time = needed / 0` → `ZeroDivisionError`。

**已实测**：`rpm=0` 时 `acquire` 直接崩溃。而「配置 0 表示禁用限流」是用户最自然的表达。

**建议**：`RateLimiterManager.get` 对 0 值跳过限流（直接放行），或在 TokenBucket 对 `refill_rate <= 0` 防御。

### 问题 2（中）：持锁 sleep

**位置**：[rate_limiter.py:50-63](app/services/llm/rate_limiter.py#L50-L63)

**影响**：等待期间锁被持有，其他排队请求无法并行计算等待时间；长 sleep 阻塞短等待请求；sleep 期间无法响应取消。

**实测边界**：单桶总耗时由 refill_rate 支配，10 并发各 0.1s 总 1.0s，未出现放大。属理论缺陷，非紧急。

**改进方向**：锁内计算 → 锁外 sleep → 循环重检。需处理 sleep 后 token 被抢的公平性，改动需谨慎。

### 问题 3（中）：TPM 桶只算 prompt token

**位置**：`_count_prompt_tokens`（llm_service.py）

**影响**：`estimated_tokens` 不含输出 token（completion），输出大时 TPM 桶低估实际消耗。

**建议**：可加 `max_tokens` 余量，或保持 prompt-only 的保守估算（当前取舍）。

### 问题 4（低）：`acquire` 返回值表述不准确

**位置**：[rate_limiter.py:102](app/services/llm/rate_limiter.py#L102) docstring「总等待时间」

**问题**：实际返回 `wait1 + wait2`（桶内等待），**不含** `retry_after` 的 sleep。docstring 措辞「总等待时间」与实现不符；调用方集成点也忽略了返回值。

**建议**：docstring 改为「桶内等待时间（不含 retry_after）」，或让返回值涵盖完整等待。

### 问题 5（低）：`async with` 用法误导

**位置**：[rate_limiter.py:113-118](app/services/llm/rate_limiter.py#L113-L118) + 顶部 docstring

**问题**：docstring 主推 `async with limiter:`，但 `__aenter__` 调 `acquire()` 无参（estimated_tokens=0，TPM 桶退化）；`__aexit__` 空操作无释放语义。实际集成点都直接 `await acquire(estimated_tokens=...)`，docstring 与实际脱节。

**建议**：docstring 改为直接 `await acquire()` 为唯一用法，`async with` 标注「仅简化场景」。

### 问题 6（低）：`_tokens` 可轻微为负

**位置**：[rate_limiter.py:62](app/services/llm/rate_limiter.py#L62)

**问题**：sleep 后直接 `-= tokens`，浮点时序可能产生负零点几。当前无害，可加 `max(0, ...)` 加固。
