# RetryHandler 设计文档

> **模块**：`app/services/llm/retry.py`
> **职责**：LLM API 调用的重试、熔断与降级

---

## 目录

- [设计目标](#设计目标)
- [核心概念解释](#核心概念解释)
- [架构总览](#架构总览)
- [组件详解](#组件详解)
  - [RetryConfig — 重试配置](#retryconfig--重试配置)
  - [CircuitBreaker — 熔断器](#circuitbreaker--熔断器)
  - [RetryHandler — 重试执行器](#retryhandler--重试执行器)
  - [classify_error — 错误分类](#classify_error--错误分类)
- [执行流程](#执行流程)
- [讨论与决策](#讨论与决策)
- [配置项清单](#配置项清单)
- [边界情况](#边界情况)
- [附录：问题 1 改造记录（工业级熔断判定）](#附录问题-1-改造记录工业级熔断判定)

---

## 设计目标

1. **自动恢复**：临时故障（超时、5xx）自动重试，无需上层感知
2. **自适应保护**：连续故障时熔断，防止对已宕机的下游发无用请求
3. **优雅降级**：主模型不可用时自动切换备用模型，保证服务不中断
4. **拒绝羊群效应**：指数退避 + 随机抖动，避免多并发同时重试

---

## 核心概念解释

### 指数退避（Exponential Backoff）

每次重试的等待时间按指数增长：

```
delay = base_delay × 2^attempt
```

- attempt=0 → 1s
- attempt=1 → 2s
- attempt=2 → 4s
- attempt=3 → 8s

上限由 `max_delay` 控制（默认 30s）。

**直觉**：第一次失败可能是瞬时的，短等即可；连续失败说明问题更严重，给对方更长的恢复时间。

### 随机抖动（Jitter）

在退避延迟上叠加随机值：

```
delay = random.uniform(0, base_delay × 2^attempt)
```

**为什么需要**：没有抖动的退避算法中，多个同时失败的请求会在完全相同的时刻重试（t=1s, t=2s, t=4s...），制造出周期性的流量尖峰，叫"羊群效应"（thundering herd）。抖动将重试时间打散，降低对下游的瞬时压力。

### 熔断器（Circuit Breaker）

参考电路断路器的概念，三种状态：

```
CLOSED（关闭/正常）──窗口错误率≥阈值 或 全部失败≥样本 ──→ OPEN（开启/熔断）
    ↑                                                    │
    │                                                    │ recovery_timeout 超时
    │                                                    ▼
    │                                               HALF_OPEN（半开/探测）
    └── 连续 N 次探针都成功 ────────────────────┘
           任一次探针失败 ─────────────────────→ OPEN（继续熔断）
```

- **CLOSED**：正常状态，所有请求放行。熔断判定基于**滑动时间窗口**内的错误率（参考 Hystrix 模型）：窗口内总请求达到最小请求量且错误率 ≥ 阈值 → 熔断；或窗口内**全部失败**且失败数 ≥ 样本下限（低流量纯失败保护）→ 熔断
- **OPEN**：熔断状态，请求快速拒绝（有 fallback 则走纯兜底，无则抛 `CircuitBreakerOpenError`），不调用 API
- **HALF_OPEN**：恢复探测状态，放行 `N` 个探针请求，要求**全部连续成功**才恢复，任何一个失败则回到 OPEN

### 半开探针（Half-Open Probe）

熔断后，系统需要知道下游是否已恢复。`recovery_timeout` 超时后进入半开状态，放行 `half_open_max_requests` 个探针请求：

- **所有探针全部连续成功** → 熔断器关闭，恢复正常
- **任何一个探针失败** → 熔断器重新开启，计时器重置，已积累的成功计数清零
- **探针收到 429**：不计入熔断状态机——限流不代表未恢复，本次探测不改变熔断状态（保持 HALF_OPEN）

每个探针都是**单次调用**（走 `_probe_attempt`，不进入重试循环）——一次探测失败若被重试放大成多次调用，会干扰对下游恢复状态的判断。

`half_open_max_requests`（默认 3）要求多次验证后才恢复，避免单个探针因网络抖动导致误恢复。

### 降级 / Fallback

主模型全部重试失败后，尝试调用备用模型（如 gpt-4o → deepseek-chat）：

```
主模型 call_fn → 重试 N 次 → 全部失败
    → fallback_fn（备用模型）→ 成功 → 直接返回（不触碰熔断器）
    → fallback_fn 也失败 → 抛出最后一次异常
```

Fallback 的目的是**保证服务不中断**——响应可能不是最好的模型生成的，但总比没有响应好。

> **关键约束**：fallback 是**纯兜底**，其成败**完全不进入熔断状态机**（不调用 `record_success`/`record_failure`）。熔断器只观察主链路（`call_fn`）的健康：备用链路通不能证明主链路恢复，备用链路故障也不代表主链路故障。主链路的故障—恢复判定只由主链路自身给出：CLOSED 下主链路成功向滑动窗口追加成功记录，错误率随之自然回落；熔断（OPEN）后，恢复只能由半开探针验证。

### 羊群效应（Thundering Herd）

多个并发请求同时失败后，在相同的退避时间点同时重试，形成周期性的"冲击波"。抖动（jitter）通过随机化重试时间打破这种同步。

---

## 架构总览

```
                 RetryHandler
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   RetryConfig  CircuitBreaker  classify_error
        │            │            │
   max_retries  window_seconds   超时→RETRYABLE
   base_delay   error_threshold  5xx→RETRYABLE
   max_delay    request_volume   429→RATE_LIMITED
   use_jitter   all_failed_min   4xx→NON_RETRYABLE
```

### 分层关系

| 层             | 组件                       | 职责                                 |
| -------------- | -------------------------- | ------------------------------------ |
| 配置层         | `RetryConfig`              | 重试参数（次数、退避、抖动）         |
| 保护层         | `CircuitBreaker`           | 熔断状态机（关闭/开启/半开）         |
| 判定层         | `classify_error()`         | 异常分类（可重试/致命/限流）         |
| 编排层         | `RetryHandler`             | 整合上述三者的主循环                 |

---

## 组件详解

### RetryConfig — 重试配置

```python
@dataclass
class RetryConfig:
    max_retries: int = 2        # 最大重试次数（不含首次）
    base_delay: float = 1.0     # 退避基数（秒）
    max_delay: float = 30.0     # 退避上限（秒）
    use_jitter: bool = True     # 是否启用随机抖动
```

**关于 max_retries**：值为 2 时实际最多执行 3 次（首次 + 重试 2 次）。这是与 `range(max_retries + 1)` 的配合约定。

### CircuitBreaker — 熔断器

```python
@dataclass
class CircuitBreaker:
    window_seconds: float = 10.0             # 滑动时间窗口长度（秒）
    error_threshold: float = 0.5             # 窗口内错误率熔断阈值（50%）
    request_volume_threshold: int = 20       # 窗口内最小请求量，不足则不做错误率评估
    all_failed_min: int = 3                  # 低流量纯失败保护：全部失败且达此样本量才熔断
    recovery_timeout: float = 30.0           # 熔断持续秒数后进入半开
    half_open_max_requests: int = 3          # 半开状态最大探针数
```

**关键方法**：

| 方法                         | 触发时机             | 行为                                                                                                             |
| ---------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `allow_request()`            | 每次执行前           | CLOSED/探针→True；OPEN→False；半开耗尽→False                                                                     |
| `record_success()`           | 请求成功             | 窗口追加成功；HALF_OPEN 下累计连续成功，达探针阈值才关闭；**OPEN 下 no-op**（见下）                              |
| `record_failure()`           | 请求失败             | 返回 `bool`；窗口追加失败并评估（错误率/全失败）→OPEN；半开失败→OPEN 并清空成功；OPEN 下 no-op（见下）           |

**状态机细节**：

- `OPEN→HALF_OPEN` 切换时，当前请求作为第一个探针（`half_open_requests=1`），所以 `half_open_max_requests=3` 时共放行 3 个探针，第 4 个拒绝
- `HALF_OPEN` 下需要**全部探针连续成功**才关闭熔断器（`_consecutive_successes ≥ half_open_max_requests`），任何一个失败则回到 OPEN 并清空成功计数
- CLOSED 下的 `record_success()` 向滑动窗口追加一条成功记录（`_window`），不直接改变状态——错误率随窗口滑动自然回落
- **OPEN 下的 `record_success()` 是 no-op**（防御）：正常路径下 OPEN 不会执行 call_fn（`allow_request()` 会拒绝它），此状态收到成功只能来自外部误调用，不得据此关闭熔断器
- CLOSED 下的 `record_failure()` 向窗口追加一条失败记录并**评估**：窗口内错误率达标（且请求量达门槛）或全部失败（且达样本下限）→ OPEN。返回 `True` 表示本次失败**触发了 OPEN**
- **请求级粒度**：一次 `execute()` 的多次重试失败只调用一次 `record_failure()`（重试耗尽后统一记录），避免单请求的重试放大窗口统计。429 / 不可恢复错误不记录
- **`_last_failure_time` 只在熔断器"进入 OPEN"那一刻更新**（冷却期起点）。OPEN 期间的延续失败不再改写它——否则 `allow_request()` 的 `now - _last_failure_time >= recovery_timeout` 判定被反复推迟，熔断器永远进不了 HALF_OPEN，下游已恢复也探测不到。探针失败（HALF_OPEN→OPEN）属于新一轮故障，重置冷却计时。fallback 与熔断器隔离（不调用 `record_failure`），429 也不记录，所以 OPEN 期间的失败来源已不存在——该守卫是防御性保护

### RetryHandler — 重试执行器

```python
class RetryHandler:
    def __init__(self, config=None, circuit_breaker=None):
        self.config = config or RetryConfig()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

    async def execute(self, call_fn, fallback_fn=None) -> Any:
```

**设计要点**：

- `call_fn` 和 `fallback_fn` 都是 `Callable[[], Awaitable[Any]]`——零参数的可等待函数，通过闭包捕获上下文
- 熔断检查在**重试循环之前**：CLOSED 放行进重试循环；HALF_OPEN 走单次探针（`_probe_attempt`）；OPEN（或半开探针占满）拒绝主调用——有 fallback 则走纯兜底，无则抛 `CircuitBreakerOpenError`
- 重试循环**仅 CLOSED 下执行**：失败按类别处理——`NON_RETRYABLE` 直接抛出，`RATE_LIMITED` 退避重试（尊重 `Retry-After`），`RETRYABLE` 退避重试
- **请求级熔断记录**：重试耗尽后，若本次请求**任一次尝试**出现过 `RETRYABLE` 失败（超时/5xx），统一调用一次 `cb.record_failure()`（可能触发 OPEN）；纯 429 / 不可恢复错误不记录。判定看"整个请求是否触及下游故障"，而非最后一次异常——混合 429 与超时时，只要出现过超时就计入窗口。`record_failure()` 返回 `True` 表示本次失败触发了熔断（影响后续请求放行）
- fallback 是**纯兜底**：成功/失败都不触碰熔断器（不调用 `record_success`/`record_failure`）——熔断器只观察主链路 `call_fn` 的成败

### classify_error — 错误分类

```python
RETRYABLE      # 超时、5xx → 重试 + 计入熔断窗口
RATE_LIMITED   # 429 → 退避重试（尊重 Retry-After），不计入熔断窗口
NON_RETRYABLE  # 400/401/403/422 → 直接抛出，不重试、不记录
```

**分类规则**：

| 异常 / HTTP 状态                           | 分类                          | 处理                                                 |
| ------------------------------------------ | ----------------------------- | ---------------------------------------------------- |
| `TimeoutError` / `APITimeoutError`         | RETRYABLE                     | 重试 + 计入熔断窗口                                  |
| 5xx（500-599）                             | RETRYABLE                     | 重试 + 计入熔断窗口                                  |
| 429 / `RateLimitError`                     | RATE_LIMITED                  | 退避重试（尊重 Retry-After），**不计入熔断**         |
| 400 / 401 / 403 / 422                      | NON_RETRYABLE                 | 直接抛出不重试                                       |
| 其他异常                                   | RETRYABLE（保守兜底）         | 重试 + 计入熔断窗口                                  |

---

## 执行流程

### 完整流程图

```mermaid
flowchart TB
    START(["execute(call_fn, fallback_fn)"])
    --> CHECK_CB["CircuitBreaker.allow_request()"]

    CHECK_CB -- "CLOSED" --> PASS[("✅ 放行进重试循环")]

    CHECK_CB -- "HALF_OPEN（有空位）" --> PROBE_ENTRY["_probe_attempt<br>单次探针"]

    CHECK_CB -- "OPEN / 半开占满" --> CB_REJ{"有 fallback?"}
    CB_REJ -- "否" --> OPEN_ERR["raise CircuitBreakerOpenError"]
    OPEN_ERR --> END_REJ(["❌ 请求被拒绝，未执行 call_fn"])
    CB_REJ -- "是" --> FB_OPEN["fallback_fn()<br>（纯兜底，不触碰熔断器）"]
    FB_OPEN --> FB_OPEN_RET(["↩ fallback 结果 / 抛异常"])

    subgraph PROBE[半开探针：单次调用主链路，不做重试]
        PROBE_ENTRY --> P_CALL["call_fn()"]
        P_CALL -- "✅ 成功" --> P_OK["record_success()<br>连续成功 ≥ 3 → CLOSED"]
        P_OK --> P_RET(["↩ 返回结果"])
        P_CALL -- "❌ 致命异常" --> P_FB["fallback_fn()<br>（纯兜底）"]
        P_CALL -- "❌ 429 限流" --> P_RL["不计入熔断<br>（保持 HALF_OPEN）"]
        P_RL --> P_FB
        P_CALL -- "❌ 其他异常" --> P_FAIL["record_failure()<br>→ 回 OPEN，冷却重置"]
        P_FAIL --> P_FB
        P_FB --> P_RET
    end

    PASS --> LOOP{"重试循环<br>attempt ∈ [0, max_retries]"}

    LOOP -- "进入 attempt" --> CALL["call_fn()"]

    CALL -- "✅ 成功" --> SUCCESS["record_success()<br>窗口追加成功"]
    SUCCESS --> RETURN(["↩ 返回结果"])

    CALL -- "❌ 异常" --> CLASSIFY["classify_error(e)"]

    CLASSIFY -- "NON_RETRYABLE<br>(400/401/403/422)" --> THROW["raise e<br>（不重试、不记录）"]
    THROW --> END_FATAL(["❌ 抛出异常，流程结束"])

    CLASSIFY -- "RATE_LIMITED（429）" --> RL_DELAY["退避 delay<br>= max(指数退避, Retry-After)<br>（不计入熔断窗口）"]

    CLASSIFY -- "RETRYABLE<br>（超时/5xx）" --> RL_DELAY

    RL_DELAY --> RL_WAIT["await asyncio.sleep(delay)"]
    RL_WAIT --> RL_LAST{"attempt ≥ max_retries?"}
    RL_LAST -- "否" --> LOOP
    RL_LAST -- "是" --> REQUEST_REC["请求级统一记录<br>record_failure()<br>（仅 RETRYABLE，一次）"]

    subgraph CB_EVAL[熔断评估]
        REQUEST_REC --> EVAL["窗口追加失败并评估<br>全部失败≥样本 或 错误率≥阈值"]
        EVAL -- "未触发" --> NOT_OPEN["保持 CLOSED<br>（不改写冷却计时）"]
        EVAL -- "触发" --> TO_OPEN["_state = OPEN<br>_last_failure_time = now<br>（冷却起点）"]
    end

    NOT_OPEN --> FALLBACK{"fallback_fn 是否存在?"}
    TO_OPEN --> FALLBACK

    FALLBACK -- "否" --> RAISE_LAST["raise last_exc"]
    RAISE_LAST --> END_FAIL(["❌ 所有重试失败，抛出异常"])

    FALLBACK -- "是" --> FB_CALL["fallback_fn()<br>（纯兜底，不进入熔断状态机）"]

    FB_CALL -- "✅ 成功" --> FB_OK["直接返回<br>（不清零熔断窗口）"]
    FB_OK --> FB_RET(["↩ fallback 结果"])
    FB_CALL -- "❌ 失败" --> FB_FAIL["raise last_exc<br>（不累计熔断窗口）"]
    FB_FAIL --> FB_END(["❌ fallback 也失败"])
```

### 熔断器内部状态详解

熔断器内部维护三块状态（滑动窗口模型下**没有"熔断计数"概念**，改为窗口记录 + 状态变量）：

| 状态项                 | 字段                               | 更新时机                                                                             | 清除时机                                                |
| ---------------------- | ---------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------- |
| 窗口请求记录           | `_window`（deque）                 | 每次请求级 `record_success()`/`record_failure()` 追加 `(ts, 成败)`；429 不计入       | 窗口滑动过期剔除；熔断关闭 → `clear()`；`reset()`       |
| 半开探针计数           | `_half_open_requests`              | `allow_request()` 放行探针时 +1                                                      | `record_success()` → 0；`OPEN→HALF_OPEN` → 1            |
| 连续成功计数           | `_consecutive_successes`           | 半开下每次 `record_success()` +1                                                     | 熔断关闭时 → 0；半开中失败 → 0                          |

### 场景推演：一次完整的"熔断-恢复"周期

以下推演使用默认配置：`max_retries=2`、`window_seconds=10`、`request_volume_threshold=20`、`error_threshold=0.5`、`all_failed_min=3`、`half_open_max_requests=3`。

```
熔断器初始状态：CLOSED, _window=[]

─────────────────────────────────────────
请求 A（第 1 次请求）
─────────────────────────────────────────
  attempt=0 → call_fn() → ❌ 超时（RETRYABLE）
    退避 ~1s
  attempt=1 → call_fn() → ❌ 超时
    退避 ~2s
  attempt=2 → call_fn() → ❌ 超时
    重试耗尽 → 请求级统一记录：任一次尝试是超时（RETRYABLE）→ record_failure() 一次 → _window=[(T,✗)]
    评估：failures=1, total=1 → 全部失败但 < all_failed_min(3) → 不熔断
  → 无 fallback → raise last_exc

熔断器状态：CLOSED, _window=[✗]

─────────────────────────────────────────
请求 B（第 2 次请求）
─────────────────────────────────────────
  attempt=0 → call_fn() → ❌ 超时
    退避 ~1s
  attempt=1 → call_fn() → ❌ 超时
    退避 ~2s
  attempt=2 → call_fn() → ❌ 超时
    重试耗尽 → record_failure() → _window=[✗, ✗]
    评估：failures=2, total=2 → 全部失败但 < 3 → 不熔断
  → 无 fallback → raise last_exc

熔断器状态：CLOSED, _window=[✗, ✗]

─────────────────────────────────────────
请求 C（第 3 次请求）
─────────────────────────────────────────
  重试耗尽 → record_failure() → _window=[✗, ✗, ✗]
  评估：failures=3, total=3 → 全部失败且 ≥ all_failed_min(3) → 触发 OPEN！
  （低流量纯失败保护：请求量不足门槛也能熔断）
  ★ record_failure() 返回 True，熔断器切到 OPEN，_last_failure_time = T3
  → 无 fallback → raise last_exc

熔断器状态：OPEN, _last_failure_time=T3

─────────────────────────────────────────
请求 D ~ G（第 4~7 次请求，熔断持续中）
─────────────────────────────────────────
  每次 allow_request() → OPEN → False
  ← 全部抛出 CircuitBreakerOpenError
  ★ 不执行 call_fn，不消耗 API 配额
  ★ 窗口统计冻结（请求未到 record_failure 环节就被拒绝）

熔断器状态：OPEN（T3 开始计时）

─────────────────────────────────────────
T3 + 30s 后 → 请求 H（探针 #1）
─────────────────────────────────────────
  allow_request()：
    检测到 now - T3 ≥ recovery_timeout(30s)
    → _state = HALF_OPEN
    → _half_open_requests = 1
    → 放行（探针 #1）

  单次调用（探针不进入重试循环）→ call_fn() → ✅ 成功！
  record_success() → _consecutive_successes=1  ← 1/3
  ↩ 返回结果

熔断器状态：HALF_OPEN（还需 2 次成功）

─────────────────────────────────────────
请求 I（探针 #2）
─────────────────────────────────────────
  allow_request() → HALF_OPEN, 有空位 → 放行

  单次调用（探针不进入重试循环）→ call_fn() → ✅ 成功！
  record_success() → _consecutive_successes=2  ← 2/3
  ↩ 返回结果

熔断器状态：HALF_OPEN（还需 1 次成功）

─────────────────────────────────────────
请求 J（探针 #3）
─────────────────────────────────────────
  allow_request() → HALF_OPEN, 有空位 → 放行

  单次调用（探针不进入重试循环）→ call_fn() → ✅ 成功！
  record_success() → _consecutive_successes=3 ≥ 3
    → _state = CLOSED
    → _window.clear()，_half_open_requests / _consecutive_successes 清零
  ↩ 返回结果

熔断器已恢复：CLOSED，窗口清空

★ 如果探针 #2 或 #3 中任一个失败：
  record_failure() → _state = OPEN, _consecutive_successes = 0
  熔断器重新开启，等待下一个 recovery_timeout
  （若配置了 fallback：探针失败后调用 fallback_fn 纯兜底返回给用户，
   不触碰熔断器——熔断器只观察主链路探针的成败）

★ 探针收到 429：不计入熔断状态机，保持 HALF_OPEN——
  限流不代表下游未恢复，不能把探针打回 OPEN。

★ 探针失败后的流程（问题 4 修复）：探针走**单次调用**路径
  （`_probe_attempt`），不做重试。修复前探针失败 → OPEN 后仍继续重试，
  若重试恰好成功，`record_success()` 在 OPEN 下会误把熔断器关回 CLOSED——
  下游实际未恢复，后续请求全部放行打向故障服务。修复后：
  ① 探针不重试，失败即确认未恢复、回 OPEN；
  ② `record_success()` 在 OPEN 下为 no-op（防御），无法关闭熔断器。
  双重防护。
```

### 关键边界说明

**熔断判定基于滑动窗口错误率 + 低流量纯失败保护**（问题 1 实施）。CLOSED 下每次请求完成后评估：窗口内总请求达门槛且错误率 ≥ 阈值 → 熔断；或窗口内**全部失败**且失败数 ≥ `all_failed_min` → 熔断。请求量不足门槛且未全部失败时保持 CLOSED（防低流量误判）。

**请求级粒度**：一次 `execute()` 的多次重试失败只调用一次 `record_failure()`（重试耗尽后统一记录），`record_failure()` 返回 `True` 表示本次失败把熔断器切到了 OPEN（影响后续请求放行，不再有"当前请求剩余重试"的概念）。

**429 不计入熔断**：限流只退避重试（尊重服务端 `Retry-After`），不进入窗口统计。探针收到 429 也不改变熔断状态。

**熔断期间熔断器统计与冷却计时保持冻结**。熔断 OPEN 时 `allow_request()` 拒绝主调用（有 fallback 则走纯兜底），`call_fn` 从未被执行，`record_failure()` 不会被正常调用；即便外部误调用，OPEN 下 `record_failure()` 也是 **no-op**（不追加窗口、不改写 `_last_failure_time`）。所以窗口统计在熔断期间保持不变，直到 `record_success()` 将其清空（熔断关闭）。

### 修复记录（问题 1 / 2 / 4）

| 问题                                                      | 修复前                                                                                                                                            | 修复后                                                                                                                           |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| 问题 1：计数粒度错位，熔断极易触发                        | 计数单位是单次 `call_fn()`，一次请求的多次重试被放大累计（`threshold=5, max_retries=2` 时约 2 次请求即熔断）                                      | **请求级粒度**：一次 `execute()` 只记录一次结果，避免单请求重试放大窗口统计                                                      |
| 问题 1：无时间维度，连续失败永久累计                      | 失败计数从 CLOSED 起只增不减，5 次失败分布在 1 秒或 10 分钟同样熔断                                                                               | **滑动时间窗口**（默认 10s）：窗口内统计请求数与错误率，过期记录惰性剔除，错误率随窗口自然回落                                   |
| 问题 1：429 混入熔断判据                                  | `RATE_LIMITED` 计入 `_failure_count`，限流期误熔断                                                                                                | **429 分离**：不计入窗口，只退避（尊重服务端 `Retry-After`）                                                                     |
| 问题 2：熔断触发后仍继续剩余重试                          | `record_failure()` 触发 OPEN 后重试循环照常跑完剩余 attempt，浪费配额、延迟放大                                                                   | `record_failure()` 返回 `bool`，触发 OPEN 时立即 `break`                                                                         |
| 问题 4a：半开探针进入重试循环                             | 探针失败 → OPEN → 继续重试，把一次探测放大成多次调用，干扰恢复判断                                                                                | 半开状态走 `_probe_attempt`：单次调用，失败即确认未恢复                                                                          |
| 问题 4b：OPEN 下 `record_success()` 误关熔断              | OPEN 下收到成功（重试泄漏 / fallback）走"重置为 CLOSED"兜底分支，熔断器被误关                                                                     | OPEN 下 `record_success()` 为 no-op                                                                                              |
| 修复补充：`_last_failure_time` 被延续失败反复刷新         | 任何失败都刷新 `_last_failure_time`，OPEN 下 fallback 兜底失败把冷却期无限推迟，熔断器永远无法进入 HALF_OPEN                                      | `_last_failure_time` 仅在熔断器进入 OPEN 时更新（冷却期起点）；OPEN 下延续失败不改写，探针失败（新一轮故障）重置                 |
| 问题 4 深化：fallback 成败不进入熔断状态机                | fallback 成功清零熔断计数（主链路持续故障永不熔断）；fallback 失败累计熔断计数；熔断 OPEN 期 fallback 被当作主链路传入单次调用路径                | fallback 纯兜底：成功直接返回、失败自然抛出，不调用 `record_success`/`record_failure`，熔断器只观察主链路 `call_fn`              |
| 审核补充：OPEN 下 `record_failure()` 未冻结计数           | OPEN 守卫在 `_failure_count += 1` **之后**，外部误调用（或防御路径）会继续累加计数、破坏"熔断期间冻结"语义，熔断消息里的"连续失败"数失真          | OPEN 守卫前置到累加之前：OPEN 下不追加窗口、不改写 `_last_failure_time`，返回 `False`                                            |

对应测试：`tests/unit/test_retry.py`（21 个用例）。

---

## 讨论与决策

### Q1: RETRYABLE（超时/5xx）为什么计入滑动窗口的错误率分子？

**因为超时/5xx 是"下游故障"的直接证据**，熔断器存在的意义就是识别并规避这类故障。

若把 RETRYABLE 排除在错误率之外，窗口内只会统计成功请求，错误率恒为 0——即便下游 5xx 成片，熔断器也不会打开，流量持续打到宕机的服务。所以超时/5xx 必须计入错误率分子（作为失败），驱动错误率抬升 → 触发熔断。

对比而言，**429 不计入窗口**（详见附录「429 分离」）：429 是"客户端触发自身限额"，不是下游故障证据，只退避。

> 相关：`classify_error()` 的 `RETRYABLE` 语义——「计入窗口失败 + 退避重试」；`RATE_LIMITED` 语义——「不计入窗口 + 退避重试」。

### Q2: Fallback 的成功/失败要反映到滑动窗口吗？

**不要。fallback 的成败完全不触碰滑动窗口**（fallback 隔离契约，问题 4 修复后）。

熔断器只观察主链路（`call_fn`）的健康。fallback 是备用链路，纯兜底：

- **成功**：说明备用链路可用，返回给用户即可。不向窗口追加成功记录——否则主链路持续故障时，每次都被 fallback 救场追加成功、把错误率稀释回正常，熔断器永远不会打开
- **失败**：说明备用链路也不可用。不向窗口追加失败记录、不改写冷却计时——备用链路的故障不是主链路故障的证据

> **修复前**：fallback 成功被当作主链路成功记入熔断（乐观恢复），失败被当作主链路失败记入。这导致：① 主链路持续故障但熔断永不触发（错误率被 fallback 救场稀释）；② 备用链路故障被误记到主链路熔断器；③ 熔断 OPEN 期 fallback 被当作主链路传入单次调用路径，备用链路的失败累计进主链路冷却期。

### Q3: max_retries 与熔断参数（window / error_threshold / request_volume / half_open）有什么关系？一般怎么设置？

熔断判定已改为滑动窗口错误率模型（问题 1 实施，参考 Hystrix），各参数在故障生命周期中控制**不同阶段**：

```
时间轴：  首次失败 → 重试 → 重试 → ... → 熔断开启 → 等待 recovery → 半开探针 → 关闭/继续熔断
           ├── max_retries 控制 ──┤
                                          ├ window_seconds + error_threshold 控制 ┤
                                                                  ├ half_open_max_requests ┤
```

- **`max_retries`** — 单次请求的"挣扎"次数。控制一个请求在放弃前尝试几次
- **`window_seconds`** — 熔断评估的时间范围。窗口内累计请求数与失败数，过期记录剔除
- **`error_threshold`** — 窗口内错误率阈值。总请求达标时，错误率 ≥ 阈值 → 熔断
- **`request_volume_threshold`** — 最小请求量门槛。窗口内请求数不足时不做错误率评估（防低流量误判）
- **`all_failed_min`** — 低流量纯失败保护。窗口内**全部失败**且失败数达此值 → 熔断（即使请求量不足门槛）
- **`half_open_max_requests`** — 恢复时的"验证"数量。控制半开状态下放行几个探针验证下游是否恢复

#### 关系 1：`max_retries` 与熔断判定解耦（问题 1 核心改进）

修复前 `failure_threshold` 按**单次 `call_fn()` 失败**累计（一次请求的多次重试被放大成多个失败信号，`threshold=5, max_retries=2` 时约 2 次请求即熔断）。修复后**记录粒度是请求级**：一次 `execute()` 只向窗口追加一条记录（`record_success()` 或 `record_failure()`），单请求的多次重试不再放大错误率。所以 `max_retries` 与熔断判定**互不影响**——重试次数再多，也只贡献一条窗口记录。

#### 关系 2：`window_seconds + error_threshold` 决定熔断灵敏度

- 窗口越短、错误率阈值越低 → 对故障越敏感，但也越容易受偶发抖动影响
- 窗口越长 → 统计越平滑，但对持续故障的反应越慢
- 默认 `10s + 50%` 是工业常用起点：半数是请求失败的窗口即熔断

#### 关系 3：`request_volume_threshold` 与 `all_failed_min` 是防误判的互补机制

- **高流量**下主要靠 `request_volume_threshold` + `error_threshold`：请求量充足时按错误率判断
- **低流量**下 `request_volume_threshold` 永远达不到 → 靠 `all_failed_min`：全部失败且达最小样本量即熔断，避免"连续失败却永不熔断"
- 两者互补：一个防高流量误判，一个防低流量漏判

#### 关系 4：`half_open_max_requests` 决定恢复速度 vs 稳定性的权衡

- **太小（1）**：一个探针成功就恢复，但如果探针刚好走运（网络抖动），恢复后立刻被正常请求打爆 → 频繁开关
- **太大（10）**：半开期放行大量探针，如果下游仍故障，这些探针都白费 → 恢复慢 + Token 浪费
- 一般 3~5 之间。熔断越保守（错误率阈值高）→ 半开探针可多一些，恢复时更确信下游已恢复

#### 典型组合策略

| 场景                   | max_retries   | window_seconds   | error_threshold   | request_volume   | all_failed_min   | half_open   | 理由                                                         |
| ---------------------- | ------------- | ---------------- | ----------------- | ---------------- | ---------------- | ----------- | ------------------------------------------------------------ |
| **默认保守**           | 2             | 10               | 0.5               | 20               | 3                | 3           | 高流量按错误率 50% 熔断，低流量全部失败 3 次即熔断           |
| **高可用/敏感**        | 1             | 5                | 0.3               | 10               | 3                | 2           | 窗口短、阈值低 → 快速熔断                                    |
| **深度容错**           | 3             | 20               | 0.7               | 30               | 5                | 5           | 窗口长、阈值高 → 尽可能多试，大面积故障才熔断                |
| **不熔断（纯重试）**   | 2             | 10               | 0.99              | 1000             | 999              | 3           | 阈值开到不可能触发，等于禁用熔断，只做重试不保护             |

当前默认值走的是**保守路线**——平衡了偶发波动和大面积故障。要更敏感就把 `window` 缩短 / `error_threshold` 降低 / `all_failed_min` 调小；要更宽松则反之。

### Q4: 为什么 `RetryConfig` 和 `CircuitBreaker` 的默认值不从代码硬编码而是从 settings 读取？

保证**运行时统一可配**。所有 LLM 子包的重试行为都由 `app/config/settings.py` 集中控制，修改 `.env` 即可调整，无需改代码。对应关系见下方配置项清单。

### Q4: 为什么不引入 `tenacity` 等第三方重试库？

- 本项目需要熔断器 + fallback + 错误分类的紧耦合编排，`tenacity` 的重试装饰器模式不适合这种控制流
- 熔断器需要跨请求共享状态（类级别），装饰器模式难以表达
- 重试逻辑本身不到 100 行，自实现更透明、易调试

---

## 配置项清单

所有配置项集中在 `app/config/settings.py`，通过 `.env` 覆盖：

| 配置项                                       | 默认值         | 说明                                           | 关联组件                                        |
| -------------------------------------------- | -------------- | ---------------------------------------------- | ----------------------------------------------- |
| `LLM_MAX_RETRIES`                            | `2`            | 最大重试次数                                   | `RetryConfig.max_retries`                       |
| `LLM_BASE_DELAY`                             | `1.0`          | 退避基数（秒）                                 | `RetryConfig.base_delay`                        |
| `LLM_MAX_DELAY`                              | `30.0`         | 退避上限（秒）                                 | `RetryConfig.max_delay`                         |
| `LLM_USE_JITTER`                             | `True`         | 是否启用随机抖动                               | `RetryConfig.use_jitter`                        |
| `LLM_CIRCUIT_WINDOW_SECONDS`                 | `10.0`         | 滑动时间窗口长度（秒）                         | `CircuitBreaker.window_seconds`                 |
| `LLM_CIRCUIT_ERROR_THRESHOLD`                | `0.5`          | 窗口内错误率熔断阈值（50%）                    | `CircuitBreaker.error_threshold`                |
| `LLM_CIRCUIT_REQUEST_VOLUME_THRESHOLD`       | `20`           | 窗口内最小请求量，不足则不做错误率评估         | `CircuitBreaker.request_volume_threshold`       |
| `LLM_CIRCUIT_ALL_FAILED_MIN`                 | `3`            | 低流量纯失败保护：全部失败且达此样本量才熔断   | `CircuitBreaker.all_failed_min`                 |
| `LLM_CIRCUIT_RECOVERY_TIMEOUT`               | `30.0`         | 熔断恢复到半开的时间（秒）                     | `CircuitBreaker.recovery_timeout`               |
| `LLM_CIRCUIT_HALF_OPEN_MAX_REQUESTS`         | `3`            | 半开状态最大探针数                             | `CircuitBreaker.half_open_max_requests`         |
| `LLM_FALLBACK_MODEL_ID`                      | `""`           | 降级备用模型 ID（空=不启用）                   | `RetryHandler.fallback_fn`                      |

---

## 边界情况

1. **熔断 OPEN 时的请求**：不执行 `call_fn`。有 `fallback_fn` 时走纯兜底（单次、不重试、不触碰熔断器），保证服务不中断；无 fallback 时抛 `CircuitBreakerOpenError`，调用方应捕获并返回降级响应或错误消息
2. **请求级熔断记录**：一次 `execute()` 的多次重试失败只调用一次 `record_failure()`（重试耗尽后统一记录），返回 `True` 表示本次失败把熔断器切到 OPEN（影响后续请求放行）。429 / 不可恢复错误不记录
3. **半开探针耗尽**：拒绝主调用（有 fallback 走纯兜底）但不改变熔断状态，直到现有探针完成（成功或失败）
4. **fallback 也失败**：抛出最后异常（可能是 fallback 的异常或主模型的异常，取决于哪个是 last_exc）；fallback 的成败不进入熔断状态机
5. **429 不计入熔断**：限流只退避重试（尊重服务端 `Retry-After`），不进入窗口统计。探针收到 429 也不改变熔断状态（保持 HALF_OPEN）
6. **OPEN 下防御性 no-op**：`record_success()`/`record_failure()` 在 OPEN 下均不修改任何状态（成功不得关闭熔断器，失败不得追加窗口、不得改写冷却计时）
7. **并发熔断状态竞争**：`CircuitBreaker` 不是线程安全的，但 `RetryHandler` 在 asyncio 单线程事件循环中运行，无并发问题
8. **`max_retries=0`**：不重试，但熔断器仍然生效。首次失败后 `record_failure()` 被调用（请求级 1 次），窗口累积

---

## 附录：问题 1 改造记录（工业级熔断判定）

> **状态：已实施（2026-08-01）**。本附录记录问题 1 的改造背景、方案与决策结论，上方正文（方法表 / 流程图 / 场景推演 / 配置项）已同步到新模型。

### 背景与现状问题

改造前熔断判据是 `_failure_count ≥ failure_threshold`，即**连续失败次数**。问题有三：

| #             | 问题                                                                                                          | 后果                                                                                                                         |
| ------------- | ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 1             | **计数粒度错位**：计数单位是单次 `call_fn()`，而一次 `execute()` 会多次调用 `call_fn()`（重试循环）           | 一次请求的多次重试失败被当作多次独立失败累计，**熔断极易触发**（`threshold=5, max_retries=2` 时约 2 次请求即熔断）           |
| 2             | **无时间维度**：失败计数从 CLOSED 起只增不减，无窗口约束                                                      | 5 次失败分布在 1 秒或 10 分钟同样触发熔断——低流量下误熔断，高流量下反应过慢                                                  |
| 3             | **429 混入熔断判据**：`RATE_LIMITED` 也计入 `_failure_count`                                                  | 429 是"客户触发自身限流"，不是"下游故障"证据，限流期误熔断                                                                   |

### 工业级参考：Hystrix 熔断模型

| 维度               | 当前实现                   | Hystrix 工业标准                                                                                       |
| ------------------ | -------------------------- | ------------------------------------------------------------------------------------------------------ |
| 判据               | 连续失败计数               | **错误率**（失败 / 窗口内总请求 ≥ 阈值，默认 50%）                                                     |
| 时间               | 无窗口                     | **滑动时间窗口**（默认 10s）                                                                           |
| 防误触发           | 无                         | **最小请求量门槛**（`requestVolumeThreshold`，默认 20/窗口）——窗口内请求量不足则不做熔断评估           |
| 计数粒度           | 单次 `call_fn()`           | 单次命令执行（请求粒度）                                                                               |
| 429                | 计入熔断                   | 单独处理（只退避，不计入错误率）                                                                       |

### 改造方案（已实施）

#### 1. 记录粒度：请求粒度（execute）替代 call_fn 粒度 ✅

- 每个 `execute()` 只向窗口汇报**一条**结果：成功在循环内 `record_success()`；失败在重试耗尽后**统一 `record_failure()` 一次**；429 / 不可恢复错误不记录
- 避免单次请求的多次重试放大窗口失败记录（同一请求内重试再多次，也只贡献一条记录）

#### 2. 滑动时间窗口：替换"从 CLOSED 起的永久计数" ✅

- 窗口统计 `[now - window, now]` 内的请求总数与失败数，窗口滑动（旧记录过期即弃）
- 实现：`collections.deque` 记录 `(timestamp, is_success)`，O(1) 追加，`_prune_window()` 惰性清理过期条目
- **本请求完成后更新统计**：窗口统计当前请求的成败，作为**后续请求**是否放行的依据

#### 3. 熔断判定：错误率 + 最小请求量门槛 + 低流量纯失败保护 ✅

```
CLOSED 下每次请求完成后：
    if 窗口内全部失败 and 失败数 ≥ all_failed_min:   # 低流量纯失败保护
        → OPEN
    elif 窗口内总请求 ≥ request_volume_threshold      # 最小请求量，防低流量误判
         and 窗口内错误率 ≥ error_threshold:           # 默认 50%
        → OPEN
```

- **主判据**：窗口内总请求达门槛（默认 20）且错误率达阈值（默认 50%）→ 熔断
- **低流量纯失败保护**（决策点 3 采纳）：请求量不足门槛时，若窗口内**全部失败**且失败数 ≥ `all_failed_min`（默认 3）→ 也熔断。避免低流量下"连续失败却永不熔断"——主判据的门槛在低流量时永远无法满足

#### 4. 429 分离：只退避，不参与熔断评估 ✅

- 429 失败**不计入错误率分母或分子**，也不计入总请求量（`record_failure()` 前排除，探针下也不记录）
- 429 失败触发**退避重试**（尊重服务端 `Retry-After`，`_extract_retry_after` + `max(delay, retry_after)`），与"下游故障"的 5xx / 超时路径彻底分离

### 新配置项（已实施）

| 配置项（settings）                               | 默认值           | 说明                                                  |
| ------------------------------------------------ | ---------------- | ----------------------------------------------------- |
| `LLM_CIRCUIT_WINDOW_SECONDS`                     | `10.0`           | 滑动时间窗口长度（秒）                                |
| `LLM_CIRCUIT_ERROR_THRESHOLD`                    | `0.5`            | 窗口内错误率熔断阈值（50%）                           |
| `LLM_CIRCUIT_REQUEST_VOLUME_THRESHOLD`           | `20`             | 窗口内最小请求量，不足则不做错误率评估                |
| `LLM_CIRCUIT_ALL_FAILED_MIN`                     | `3`              | 低流量纯失败保护：全部失败且达此样本量才熔断          |

原 `LLM_CIRCUIT_FAILURE_THRESHOLD`（连续失败计数）**已移除**（决策点 1）。

### 行为对比（示意）

```
熔断器状态：CLOSED

请求 A（窗口内第 1~3 次）→ 3 次失败，全部失败且 ≥ all_failed_min(3) → 熔断
                         （低流量纯失败保护；修复前需满 5 次才熔断）
请求 B（窗口内第 20~25 次）→ 20 次请求中 15 次失败 → 错误率 75% ≥ 50% → 熔断
请求 C（窗口内 429 增多）→ 429 不计入错误率 → 不熔断，仅退避重试
```

### 决策结论（用户 2026-08-01 确认）

1. **移除 `failure_threshold`**：由滑动窗口错误率 + 低流量纯失败保护完全替代
2. **429 退避尊重服务端时间**：`Retry-After` 感知已实现
3. **低流量纯失败保护**：窗口内全部失败且失败数 ≥ `all_failed_min`（默认 3）时熔断
