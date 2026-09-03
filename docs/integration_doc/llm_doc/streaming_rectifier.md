# StreamingRectifier 设计文档

> **模块**：`app/integration/llm/streaming_rectifier.py`
> **更新日期**：2026-09-02
> **职责**：流式整流/半流续接策略——「首 token 前中断 → 重新 create + 重新迭代（整流）」；「已产出 content 中断 → 带前缀续写（半流续接，[LLM-ADR-015](../../../adr/integration/llm/2026-09-03-mid-stream-continuation.md)）」；其余已产出中断则放弃
> **状态**：✅ 已实现
> **定位**：从 `LLMService.async_generate` 拆出的独立策略类（无状态静态类，不实例化），让 Facade 保持编排职责
> **配套**：`StreamParser`（chunk 解析）、`LLMService.async_generate`（编排，构造 `continue_fn`）、`RetryHandler`（create 阶段重试/熔断）、`llm/errors.py`（`classify_error` 整流/续接可恢复判定）

---

## 📋 目录

- [StreamingRectifier 设计文档](#streamingrectifier-设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [整流条件（\_should\_rectify）](#整流条件_should_rectify)
    - [半流续接（\_should\_continue）](#半流续接_should_continue)
    - [结算闭环（reservation）](#结算闭环reservation)
    - [熔断 feeding](#熔断-feeding)
    - [事件日志](#事件日志)
    - [失败信号透传](#失败信号透传)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [StreamingRectifier — 整流策略类](#streamingrectifier--整流策略类)
    - [RectifierContext — 会话共享状态](#rectifiercontext--会话共享状态)
    - [配置注入（register\_config）](#配置注入register_config)
  - [执行流程](#执行流程)
  - [对外接口](#对外接口)
  - [边界情况](#边界情况)
  - [配置项清单](#配置项清单)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [问题记录](#问题记录)
  - [相关文档](#相关文档)

---

## 设计目标

1. **整流策略独立**：整流重试循环、`emitted_any` 状态、熔断 feeding 与 Facade 编排正交，独立成类
2. **首 token 前才整流**：用户没看到任何输出时重试，不产生重复内容
3. **已产出不整流**：避免重复输出 / token 双倍计费 / tool_calls 残缺
4. **半流续接（LLM-ADR-015）**：已产出 content 中断 → 带前缀续写而非整轮作废；tool_calls / reasoning 中断不接续，维持放弃
5. **结算闭环**：create 成功后的中断/取消统一 settle（保留配额），硬取消 finally 兜底 settle(None)（LLM-003）
6. **熔断观察**：迭代「放弃时」且异常 RETRYABLE → 喂熔断器，感知「create 正常但流频繁中断」

---

## 核心概念解释

### 整流条件（_should_rectify）

| 条件 | 说明 |
| --- | --- |
| 首 token 前（`emitted_any=False`） | `reasoning_token` / `message_token` / `tool_call_deltas` 任一非空即置位；`finish_reason` / `usage` **不算**"首 token"（纯 usage/finish 死流仍可整流） |
| 未超整流上限 | `attempt < stream_max_retries` |
| 异常可恢复 | `classify_error`（llm/errors.py，见 [error.md](error.md)）∈ `RETRYABLE` / `RATE_LIMITED`；NON_RETRYABLE（4xx/校验错误/截断/未知）不整流 |
| 用户未取消 | `cancel_event` 未置位 |

> 全部条件满足才整流（`_should_rectify`）。

**整流重试**：重新 create + 重新迭代。**create 阶段异常绝不整流**——`retry.execute` 已决定重试/熔断/fallback，400 / `CircuitBreakerOpenError` / fallback 失败走既有错误路径。

### 半流续接（_should_continue）

半流续接解决整流覆盖不到的区间：**已产出 content（模型未收尾）后断流**。整流（从头重来）此时会重复可见输出 + 双烧，故放弃会整轮作废；续接改为**带已产出 content 作 assistant 前缀重新请求**，模型从断点续写，客户端看到连续一段（[LLM-ADR-015](../../../adr/integration/llm/2026-09-03-mid-stream-continuation.md)，与整流正交、不冲突）。

| 边界 | 说明 |
| --- | --- |
| 已产出 content | `result.content` 非空；`finish_reason` / `refusal` 为 None（模型未收尾） |
| 异常可恢复 | `classify_error ∈ RETRYABLE / RATE_LIMITED`（与整流复用） |
| 非 reasoning 流 | `reasoning_content` / `has_reasoning` 任一非空不续——DeepSeek thinking 回喂约束 + prefix 语义未验证，保守排除 |
| 无 tool_call 半成品 | 中断流 `tool_deltas` 空——partial JSON 无法跨请求续接 |
| 轮次上限 | `cont_attempt < llm_stream_max_continuations`（默认 1，独立于整流上限） |
| 用户未取消 | `cancel_event` 未置位 |

**续接请求构造**（`llm_service.continue_fn(prefix)`，消息组装在编排层、不在整流器）：`原 messages 副本 + [{"role": "assistant", "content": prefix, "prefix": True}]`——DeepSeek `prefix` 续写字段；OpenAI 兼容端点不支持时 create 失败，整流器**尽力而为**退化到放弃（部分 content 保留 + 原中断原因失败信号），行为不劣化。

**尽力而为链**：续接请求**不经 `retry.execute` / fallback**（非主干路径）；续接再断且预算余 → 带新前缀（`result.content` 最新值）再续，超预算 → 放弃（喂熔断照旧）。

**接缝重叠剥离**（`_SeamStripper`）：续接流首部若与已产 content 尾部重叠（窗口 ≤ `_SEAM_OVERLAP_LIMIT`=64 字符）剥离后再产出/累积——已发给客户端的内容不重复显示；流自然结束仍全命中重叠视为纯重放丢弃。前提 `prefix:true` 使长重复极罕见，剥离只兜小尾巴。

### 结算闭环（reservation）

| 出口 | 处理 |
| --- | --- |
| 成功读完 | `settle(actual)` 退 TPM 差 |
| 迭代中断 / 用户取消 | `settle(actual)`（请求已发出，无论整流与否） |
| 硬取消（CancelledError） | `finally` 兜底 `settle(None)` 保留配额 + 标记终态（LLM-003：请求已发出，RPM 真实消耗不退回，防配额虚增→429） |
| **settle 退款中途被取消** | `_settle_active` 捕获 `BaseException` 把**未终态 res 塞回 active**，由 `finally` 兜底 `settle(None)` 收尾（R2：settle 中断不泄漏） |

每次整流 attempt 由调用方的 `create_fn` 重新 reserve（新请求语义）。

### 熔断 feeding

流式迭代「放弃时」（不整流）且异常为 RETRYABLE → `retry.circuit_breaker.record_failure()`，让熔断器感知「create 正常但流频繁中途断开」的下游故障。整流成功不喂、NON_RETRYABLE / RATE_LIMITED / cancel 不喂。

### 事件日志

每次尝试独立计时，失败走 `success=False` + error，成功清 `error=None`（整流成功 = 1 条失败 + 1 条成功日志）。日志填充复用 `app/platform/observability/logger.py` 的 `fill_llm_event_fields`（通用 LLM 事件日志工具）。

### 失败信号透传

三个失败出口除了产出 SSE error 事件外，还会在 `StreamResult.error` 标记失败原因——供编排层（`ReActAgent`）短路决策，避免把「LLM 失败」当「空输出」空转重试（[LLM-001](../../../issues/integration/llm/2026-08-16-stream-error-propagation.md)）：

| 出口 | `result.error` | SSE 事件 |
| --- | --- | --- |
| create 异常（NON_RETRYABLE 等） | `str(e)`（截断 `_RESULT_ERROR_LIMIT`=500） | `LLM 调用失败: ...` |
| 迭代放弃（不整流） | `str(e)`（同上截断） | `流式响应中断: ...` |
| 用户取消 | `"用户取消"` | `用户取消了请求` |

**语义边界**：

- 正常空回（`finish_reason="stop"` 且 content 空）→ `error` 保持 `None`（编排层仍走「空输出重试」逻辑，不被误判为失败）
- 整流成功路径 → `error` 保持 `None`（只有最终放弃才标记）
- 半流续接成功路径 → `error` 保持 `None`；续接 create 失败 / 续接再断超预算 → 按放弃置 `error`（原中断原因，续接中断则用末次原因）
- SSE error 事件保留（前端可感知）；`result.error` 是给后端编排层的失败信号，两者独立

`error` 字段截断上限独立于日志 `[:200]`：它可能进 `AgentResult.error` → API 响应，需防异常消息携带 URL 等内部细节全量透传。

---

## 架构总览

```text
create_fn（限流闭环 reserve + create）
    → retry.execute() 保护 create 阶段
    → rectified_stream 整流/续接循环
        ├─ _drain：逐 chunk 看门狗（首包宽/空闲窄）→ 累积 + 产出事件（整流/续接共用）
        ├─ _should_rectify：判断是否整流（首 token 前）
        ├─ _abandon_path：整流不适用时的收尾（LLM-011 取消守卫 → 半流续接 → 放弃）
        ├─ _try_continuations：半流续接链（已产出 content 带前缀续写，LLM-ADR-015）
        ├─ _SeamStripper：续接流首部接缝重叠剥离
        ├─ _apply_chunk：解析 chunk → 累积 StreamResult + 产出事件
        └─ _finish_interrupted：中断收尾（settle + 日志）
```

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 策略层 | `StreamingRectifier` | 整流循环（无状态静态类）：整流判定 / emitted_any / 结算闭环 / 熔断 feeding / 事件日志 |
| 状态层 | `RectifierContext` | 整流会话共享状态（result / active / event_fields），跨 attempt 传递 |
| 编排层 | `LLMService.async_generate` | 构造 `create_fn`（限流闭环）+ 调 `rectified_stream` 产出事件 |
| 支撑层 | `RetryHandler` / `ReservationLimiter` / `StreamParser` | create 阶段重试熔断 / 结算 / chunk 解析 |

---

## 组件详解

### StreamingRectifier — 整流策略类

`rectified_stream` 是无状态静态方法（不实例化），产出 SSE 事件字符串（与 `async_generate` 的 yield 契约一致）：

```python
rectifier_context = RectifierContext(result, active, event_fields)
async for event in StreamingRectifier.rectified_stream(
    create_fn=lambda: _rate_limited_call(...),   # 每次整流 attempt 重新 reserve + create
    retry=retry,
    cancel_event=cancel_event,
    stream_max_retries=stream_max_retries,      # 由 async_generate 传入（llm_stream_max_retries 配置值）
    context=rectifier_context,                    # 会话共享状态
    fallback_fn=fallback_fn,
    continue_fn=continue_fn,                    # 半流续接请求构造（LLM-ADR-015；None=禁用）
    continuation_max_retries=continuation_max,  # 续接轮次上限（llm_stream_max_continuations 配置值）
):
    yield event
```

**整流/续接循环内部**：

- `_drain`：迭代单个流式响应——逐 chunk 看门狗（首包宽/空闲窄，LLM-ADR-014）+ `_apply_chunk` 累积 + 事件产出；迭代中用户取消置位抛内部 `_StreamCancel` 信号（与硬取消 CancelledError 区分）。整流 attempt 与续接 attempt 共用
- `_apply_chunk`：解析 chunk → 累积 `StreamResult` + 产出事件；`emitted_any` **累积语义**（`emitted_any or chunk_emitted`）——已产出标记单调递增，元数据 chunk 不冲掉历史产出（[LLM-035](../../../issues/integration/llm/2026-08-10-rectify-emitted-any-marker-reset.md)）；整流 `continue` 前清空 `tool_deltas`（[LLM-030](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)）；续接 attempt 时 content token 经 `seam` 接缝剥离
- `_should_rectify`：整流判定（见「核心概念解释·整流条件」）
- `_abandon_path`：整流不适用时的收尾路径——LLM-011 取消守卫（取消非下游故障，不喂熔断）→ 半流续接链（尽力而为，completed 即结束）→ 放弃（RETRYABLE 喂熔断 + 失败信号，部分 content 保留）；产出其 SSE 事件即整流流结束
- `_should_continue` / `_try_continuations`：半流续接判定与尽力而为续接链（见「核心概念解释·半流续接」）；`_SeamStripper` 接缝重叠剥离
- `_finish_interrupted`：中断收尾（settle + 日志）

### RectifierContext — 会话共享状态

整流会话共享的可变状态（跨 attempt 传递），由调用方构造并持有：

```python
@dataclass
class RectifierContext:
    result: StreamResult            # 累积输出
    active: dict[str, Reservation]  # 活跃 reservation（成功 settle / 失败 cancel）
    event_fields: dict[str, Any]    # 日志字段
```

### 配置注入（register_config）

流式整流退避配置由外层 `register_config()` 注入（Container 读 settings 后调用），子模块不直接依赖 settings：

```python
# 装配根（Container.initialize）读 settings 后调用，子模块零 settings 依赖
StreamingRectifier.register_config(
    base_delay=base_delay,              # 来自 settings.llm_base_delay
    max_delay=max_delay,                # 来自 settings.llm_max_delay
    use_jitter=use_jitter,              # 来自 settings.llm_use_jitter
    first_token_timeout=first_token,    # 首包阈值（宽，覆盖模型思考）
    chunk_idle_timeout=chunk_idle,      # 空闲阈值（窄，判定断流）
)
```

退避公式与 create 阶段一致：`base_delay × 2^attempt`，上限 `max_delay`，可选随机抖动。**Retry-After 叠加**：RATE_LIMITED（429）中断整流时，提取服务端 `Retry-After` 参与退避，且与 create 阶段同样封顶到 `max_delay`——合理区间 `0 < retry_after ≤ max_delay` 内尊重，超出忽略回退指数退避（防异常大值挂死，对齐 retry.py 的 `_calculate_delay` 语义）。

**首包/空闲双阈值看门狗**（LLM-ADR-014）：迭代改为逐 chunk `asyncio.wait_for(anext(stream), 阈值)`——首 chunk 用宽阈值 `first_token_timeout`（区分「模型思考慢」，不误杀），其后每 chunk 用窄阈值 `chunk_idle_timeout`（判「流已断」，不等 httpx read 整档）。超时 `TimeoutError` 走既有整流/放弃分支：首 token 前超时整流（不重复输出）、已产出后超时放弃（保留部分内容 + 失败信号）。看门狗超时的空串 `TimeoutError` 经 `_describe_exception` 回退类型名——`result.error` 以非空为失败信号（react 短路依赖），空串会把失败误当成功空回。

---

## 执行流程

```text
async_generate → rectified_stream（整流/续接循环）
    for attempt in 0..stream_max_retries：
        ├─ 整流入口守卫：cancel_event 置位 → 不再发起 reserve + create（不发新副作用）
        ├─ create_fn()（重新 reserve + create，经 retry.execute 保护 create 阶段）
        ├─ 迭代：_drain 逐 chunk wait_for 看门狗（首包宽/空闲窄）+ 累积 + 产出事件
        │    └─ 异常/看门狗超时 → _should_rectify？
        │         ├─ 是（首 token 前 + 可恢复 + 未取消）→ 退避（含 Retry-After）→ 下一 attempt 整流
        │         └─ 否（不整流）→ _abandon_path（整流不适用收尾）
        │              ├─ 取消守卫（LLM-011）：取消 → 取消事件结束（不喂熔断）
        │              └─ 半流续接（_should_continue，LLM-ADR-015）？
        │                   ├─ 是（content 已产 + 无 reasoning/tool + 可恢复 + 预算未超 + 未取消）
        │                   │    → 退避 → continue_fn(prefix=result.content) 续写（尽力而为，接缝去重）
        │                   │        ├─ 成功 → 结算 + 成功日志 + 结束
        │                   │        └─ 再断（预算内带新前缀再续）/ 预算尽 / create 失败 → 退化到放弃
        │                   └─ 否 → 放弃：熔断 feeding（RETRYABLE）+ 失败信号 + 部分 content 保留
        └─ 正常读完 / 硬取消 → 结算闭环（settle）
```

每次 attempt 独立：重新 `reserve` + `create`（新请求语义，[LLM-034](../../../issues/integration/llm/2026-08-02-quota-gap-retry-degradation-not-limited.md)）；成功读完 `settle(actual)` 退差，中断 `settle(actual)`，硬取消 `finally` 兜底 `settle(None)` 保留配额。

半流续接的每次续接 attempt 同为**新 reserve + create**（尽力而为单链，不经 `retry.execute`/fallback）；续接成功取末次完成流 usage（断流 attempt 数据不可得不计入，对齐 [LLM-039](../../../issues/integration/llm/2026-09-02-usage-accounting.md) 口径）。

---

## 对外接口

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `rectified_stream(create_fn, retry, cancel_event, stream_max_retries, context, fallback_fn, *, continue_fn=None, continuation_max_retries=0)` | 静态异步生成器 | 整流/续接循环：首 token 前中断重新 create（整流），已产出 content 中断带前缀续写（续接，尽力而为），产出 SSE 事件字符串 |
| `register_config(*, base_delay, max_delay, use_jitter, first_token_timeout, chunk_idle_timeout)` | 同步类方法 | 注入整流/续接退避 + 首包/空闲看门狗配置（keyword-only，复用 create 阶段配置，零 settings 依赖） |
| `RectifierContext(result, active, event_fields)` | dataclass | 整流会话共享状态（由调用方构造并持有） |

---

## 边界情况

1. **首 token 前中断**：`emitted_any=False` → 整流重试（重新 create + 重新迭代），用户无感
2. **已产出 token 后中断**：`emitted_any=True` → 不整流（避免重复输出 / 双倍计费 / tool_calls 残缺）
3. **create 阶段异常**：绝不整流——`retry.execute` 已决定重试/熔断/fallback
4. **异常不可恢复**（NON_RETRYABLE / 4xx / 校验错误 / 截断 / 未知）：不整流
5. **用户取消**：`cancel_event` 置位 → 不整流、不再发起新请求（[LLM-006](../../../issues/integration/llm/2026-08-16-rectify-entry-cancel-check.md)）；放弃分支喂熔断前也检查取消（[LLM-011](../../../issues/integration/llm/2026-08-16-cancel-race-feeds-breaker.md)）
6. **纯 usage/finish 死流**：`finish_reason` / `usage` 不算首 token——`emitted_any=False` 仍可整流
7. **settle 退款中途被取消**：`_settle_active` 把未终态 res 塞回 `active`，`finally` 兜底 `settle(None)` 收尾（R2：不泄漏）
8. **失败信号透传语义边界**：正常空回 / 整流成功路径 → `error` 保持 `None`（不误判失败）；SSE error 事件与 `result.error` 独立
9. **整流后死流元数据复位**：整流重试 `continue` 前复位 `finish_reason`/`usage`/`refusal` 为 `None` + 清空 `tool_deltas`——usage/finish/refusal 不算首 token（死流仍可整流），但不残留到下一尝试，避免成功尝试被死流拒答元数据污染（下游误判拒答）
10. **半流续接触发**（LLM-ADR-015）：已产出 content（模型未收尾）+ 异常可恢复 + 非 reasoning 流 + 无 tool_call 半成品 + 预算未超 + 未取消 → 带前缀续写；任一不满足维持放弃
11. **续接 create 失败**：如 OpenAI 兼容端点忽略/拒绝 `prefix` 字段 → 记日志退化放弃——部分 content 保留 + `result.error` 用**原中断原因**（对用户更贴切），行为不劣化
12. **续接再断**：预算内（`cont_attempt < llm_stream_max_continuations`）→ 带增长后的新前缀（`result.content` 最新值）再续；超预算 → 放弃（喂熔断 + 失败信号照旧）
13. **接缝重叠剥离**：续接流首部与已产 content 尾部重叠（窗口 ≤ `_SEAM_OVERLAP_LIMIT`=64 字符）剥离后再产出/累积；流自然结束仍全命中重叠视为纯重放丢弃
14. **续接成功**：`error` 保持 `None`；`usage` 取末次完成流（断流 attempt 数据不可得不计，LLM-039）

---

## 配置项清单

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `llm_stream_max_retries` | int | `1` | 流式整流重试次数（首 token 前中断才整流；`0`=禁用） |
| `llm_stream_max_continuations` | int | `1` | 半流续接轮次上限（已产出 content 中断带前缀续写；`0`=禁用，LLM-ADR-015） |
| `llm_base_delay` / `llm_max_delay` / `llm_use_jitter` | — | — | 整流/续接退避（经 `register_config` 注入，与 create 阶段共用） |

`llm_stream_max_retries` 独立于 `llm_max_retries`：create 重试（HTTP 请求级）与整流重试（已开始流式后重启）属不同故障阶段，需独立调优。`llm_stream_max_continuations` 又独立于整流上限——整流只处理「首 token 前」、续接只处理「已产出 content 后」，故障区间不同，需独立调优（`0` 各自禁用）。

---

## 测试状态

- `tests/unit/test_streaming_rectifier.py`（20 用例，直接覆盖整流/续接策略）：首 token 前中断整流 / 已产出不整流 / cancel 不整流 / 整流上限耗尽 + 熔断 feeding / 成功 settle / **硬取消 finally settle(None) 保留配额（LLM-003）** / **settle 中途取消 finally settle(None) 收尾** / 429 整流尊重 Retry-After（封顶到 max_delay）/ **整流清理复位 refusal（拒绝类死流不残留元数据）** / **半流续接成功合并 / 续接 create 失败退化放弃 / 接缝重叠剥离 / max=0 禁用 / reasoning 不续接 / tool 半成品不续接 / 续接再断预算内再续成功 / 续接再断预算尽放弃 + 熔断 feeding（LLM-ADR-015）**
- `tests/unit/test_stream_rectify.py`（23 用例，经 `LLMService.async_generate` 间接覆盖）：整流/续接/结算/事件/日志/熔断 feeding 全链路断言，含**续接请求追加 assistant 前缀消息（`prefix:True`）+ 重新 reserve 结算**
- **LLM-001 失败信号透传**：`test_stream_rectify.py` 四个失败出口补 `result.error` 断言（create 失败 / 迭代放弃 / 用户取消）；`test_agent.py` 新增 ReActAgent 遇 LLM 失败第 1 轮短路返回失败结果用例

---

## 设计决策

> 整流重试策略（首 token 前中断自动恢复）的完整决策（Context → Decision → Consequences，含工业级参照）已归档至 [ADR LLM-ADR-005](../../../adr/integration/llm/2026-08-01-streaming-rectification-retry.md)。
> 半流中断接续策略（已产出 content 带前缀续写，text-only prefix continuation）的完整决策已归档至 [ADR LLM-ADR-015](../../../adr/integration/llm/2026-09-03-mid-stream-continuation.md)。

---

## 问题记录

> 涉及整流策略的问题已提取归档，完整生命周期（发现 → 分析 → 修复 → 验证 → 教训）见：

- [流式失败信号透传（LLM-001）](../../../issues/integration/llm/2026-08-16-stream-error-propagation.md)
- [流式硬取消保留配额（LLM-003）](../../../issues/integration/llm/2026-08-16-hard-cancel-rpm-refund.md)
- [整流入口取消守卫（LLM-006）](../../../issues/integration/llm/2026-08-16-rectify-entry-cancel-check.md)
- [整流放弃分支取消守卫（LLM-011）](../../../issues/integration/llm/2026-08-16-cancel-race-feeds-breaker.md)
- [流式迭代异常无保护（熔断观察盲区，LLM-024）](../../../issues/integration/llm/2026-08-07-streaming-iteration-unprotected.md)
- [流式/非流式解析健壮性（整流幂等，LLM-030）](../../../issues/integration/llm/2026-08-07-stream-parser-robustness.md)
- [emitted_any 累积语义（LLM-035）](../../../issues/integration/llm/2026-08-10-rectify-emitted-any-marker-reset.md)

---

## 相关文档

- [LLM 层总览](llm.md)（async_generate 编排）
- [StreamParser](streaming.md)（chunk 解析，`ParsedChunk`）
- [重试与熔断](retry.md)（`RetryHandler` / 熔断 feeding）· [传输错误处理](error.md)（`classify_error` 分类）
- [限流器](limiter.md)（reserve/settle 结算闭环）
- [全局日志框架](../../platform_doc/observability/logging.md)（`fill_llm_event_fields`）
