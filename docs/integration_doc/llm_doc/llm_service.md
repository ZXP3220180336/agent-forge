# LLMService 编排设计文档

> **模块**：`app/integration/llm/llm_service.py`
> **更新日期**：2026-08-16
> **职责**：LLM 网关统一 Facade——组织 6 组件协作完成一次 LLM 调用（可靠性链 +
> 配额结算闭环 + 事件日志）
> **状态**：✅ 已实现
> **定位**：对外接口契约见 [llm.md](llm.md)（模块对外接口文档）；本文档解释
> `LLMService` **内部如何组织组件工作**（编排机制，供内部维护者 / 集成方）
> **配套**：实现领域端口 `LLMGateway`；依赖 `ClientManager` / `RetryHandler` /
> `StreamingRectifier` / `StreamParser` / `ReservationLimiter` / `StructuredOutput` /
> `CostTracker`

---

## 📋 目录

- [设计目标](#设计目标)
- [核心概念解释](#核心概念解释)
  - [可靠性链（每次调用）](#可靠性链每次调用)
  - [限流闭环（\_rate\_limited\_call）](#限流闭环_rate_limited_call)
  - [结算闭环（finally 兜底）](#结算闭环finally-兜底)
  - [整流 × 限流协作](#整流--限流协作)
  - [fallback 同 provider](#fallback-同-provider)
  - [TPM 估算](#tpm-估算)
- [架构总览](#架构总览)
- [组件详解](#组件详解)
  - [\_build\_chat\_kwargs — 请求参数构建](#_build_chat_kwargs--请求参数构建)
  - [\_build\_fallback\_fn — fallback 降级函数](#_build_fallback_fn--fallback-降级函数)
  - [\_build\_event\_fields — 事件字段构建](#_build_event_fields--事件字段构建)
  - [\_rate\_limited\_call — 限流闭环](#_rate_limited_call--限流闭环)
  - [\_count\_prompt\_tokens — TPM 估算](#_count_prompt_tokens--tpm-估算)
  - [LLMService 编排方法](#llmservice-编排方法)
- [执行流程](#执行流程)
  - [async\_generate（流式全链路）](#async_generate流式全链路)
  - [generate（非流式全链路）](#generate非流式全链路)
  - [generate\_structured（委托三级降级）](#generate_structured委托三级降级)
- [对外接口](#对外接口)
- [边界情况](#边界情况)
- [配置项清单](#配置项清单)
- [测试状态](#测试状态)
- [设计决策](#设计决策)
- [问题记录](#问题记录)
- [相关文档](#相关文档)

---

## 设计目标

1. **Facade 统一编排**：`async_generate` / `generate` / `generate_structured` 是唯一对外
   入口，调用方不直接触碰 6 组件；内部组织组件协作的细节对调用方透明
2. **可靠性链闭环**：限流（事前排队）→ 重试/熔断/降级（保护 create 阶段）→ 整流
   （流式）→ 解析 → 事件日志，一次调用走完整链路
3. **配额结算闭环**：每个 `reserve` 必配结算，`finally` 兜底防泄漏——create 失败
   `cancel()` 全额退、create 成功后 `settle(actual)` 退差 / `settle(None)` 保留
4. **流式整流与限流协作**：整流重试每轮重新进入 call_fn = 重新 `reserve` + `create`
   （新请求语义，见 [LLM-034](../../../issues/integration/llm/2026-08-02-quota-gap-retry-degradation-not-limited.md)）

---

## 核心概念解释

### 可靠性链（每次调用）

一次 LLM 调用依次经过（各组件设计见对应子文档）：

```text
ReservationLimiter（事前限流：reserve 排队，配额不足等待而非请求）
    → RetryHandler（重试/熔断/fallback：保护 create 阶段，NON_RETRYABLE 上抛）
    → StreamingRectifier（流式：整流循环） 或  StreamParser（非流式：parse_non_stream）
    → fill_llm_event_fields（llm_call 事件日志：model/tokens/duration/success）
```

限流是**事前**（proactive），重试是**事后**（reactive），两者互补：客户端限流减少触发
服务端 429，真遇到 429 由重试层尊重 `Retry-After` 兜底。

### 限流闭环（_rate_limited_call）

每次真实请求（原始调用 + retry 内部重试 + 整流重试）都重新 `reserve`：

```python
async def _rate_limited_call(adaptive, limiter, client, kwargs, active, ...):
    res = await limiter.reserve(...) if not adaptive else await limiter.reserve_adaptive(...)
    active["res"] = res
    try:
        return await client.chat.completions.create(**kwargs)
    except BaseException:      # 含 CancelledError
        await res.cancel()     # 请求未确认发出 → 全额退（RPM+TPM）
        active.pop("res", None)
        raise
```

**闭环语义**（按「请求是否已发出」分界）：

| 出口 | 方法 | 行为 |
| --- | --- | --- |
| create 失败 / 取消（请求未发出） | `cancel()` | 退 RPM + TPM 全额 |
| create 成功后一切出口（整流/取消/成功） | `settle(actual)` | 退 TPM 差（`max(0, est-actual)`），RPM 不退 |

> 为何 settle 不退 RPM：请求已真实发生，RPM 配额是真实消耗，退回会让客户端以为有配额
> 而服务端已超（触发 429）。

### 结算闭环（finally 兜底）

| 通道 | 兜底 |
| --- | --- |
| 流式（`async_generate`） | `rectified_stream` 迭代 `finally`：create 成功后的中断/取消统一 `settle(actual)`；**硬取消 `settle(None)` 保留配额 + 标记终态**（[LLM-003](../../../issues/integration/llm/2026-08-16-hard-cancel-rpm-refund.md)） |
| 非流式（`generate`） | `try/finally` 解析 + 结算：解析抛异常 → `settle(None)` 保留；`settle` 被硬取消 → 未终态 res `settle(None)` 兜底 + re-raise（[LLM-002](../../../issues/integration/llm/2026-08-16-generate-quota-settle-fallback.md)） |

**统一原则**：已发出的请求是不可回滚的已提交副作用——`cancel()` 退回 RPM 导致客户端配额
虚增 → 服务端 429 风暴，故请求发出后一切出口 `settle`，`cancel` 只用于「请求未确认发出」。

### 整流 × 限流协作

`async_generate` 的整流循环（`StreamingRectifier.rectified_stream`）每次 attempt 重新调用
`create_fn`（即 `_rate_limited_call`）——重新 `reserve` + `create`。整流重试每轮都是
**新请求**，重新扣配额（测试断言整流 2 轮 `calls["acquire"] == 2`）。

fallback（备用模型）**不参与 reserve**：备用链路防突发无意义，独立于主模型配额。

### fallback 同 provider

`_build_fallback_fn` 复用主调用构建的 `kwargs`，仅替换 `model` 为备用模型——fallback 用
主模型 client（同 base_url / 密钥）发请求，**只支持同服务商便宜模型降级**（如
deepseek-chat → deepseek-reasoner）。配置跨 provider 模型会打到主端点带备用模型名 →
400/404，fallback 静默失效（[LLM-012](../../../issues/integration/llm/2026-08-16-fallback-same-provider.md)）。

### TPM 估算

`_count_prompt_tokens` = 每条消息 +4（格式开销）+ content token + name +1，末尾 +2
（回复格式开销），+ `max_tokens` 作为输出上限的保守估算——TPM 桶按「请求可能消耗的
最大 token」扣减，宁可高估不错放。

`_content_to_text` 归一化：None → 空串；str → 原样；多模态 list（OpenAI 格式
`[{"type": "text", "text": ...}]`）→ 只取文本片段拼接，图片等非文本条目不参与估算
（避免 `encode(None)` 抛 TypeError）。

---

## 架构总览

```text
外部调用方（ReActAgent / 应用层 / API 层）
        │  经 LLMGateway 端口
        ▼
    LLMService（Facade 编排）
      ├── async_generate（流式）→ StreamingRectifier.rectified_stream（整流循环）
      │        └─ create_fn = _rate_limited_call（限流闭环：reserve → create → cancel）
      │             └─ 每次 attempt 重新 reserve（新请求语义）
      ├── generate（非流式）→ retry.execute（重试/熔断/fallback）
      │        └─ call_fn = _rate_limited_call（同上限流闭环）
      │        └─ try/finally：StreamParser.parse_non_stream + settle 结算
      ├── generate_structured → StructuredOutput.extract（三级降级，见 structure.md）
      └── calculate_cost → CostTracker.calculate
```

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 编排层 | `LLMService` | 组织各组件协作（方法分派 / 闭环控制 / 事件日志） |
| 限流层 | `_rate_limited_call` | 每次真实请求 reserve + create + cancel 兜底 |
| 可靠性层 | `RetryHandler`（经 `RetryHandlerManager.get`） | 保护 create 阶段：重试/熔断/fallback |
| 整流层 | `StreamingRectifier`（经 `rectified_stream`） | 流式整流循环（首 token 前中断重试） |
| 数据层 | `StreamParser`（经 `parse_non_stream`） | 非流式完整响应解析 |
| 结构化层 | `StructuredOutput`（经 `extract`） | 结构化输出三级降级 |
| 成本层 | `CostTracker`（经 `calculate`） | 按模型用量估算成本 |

---

## 组件详解

### _build_chat_kwargs — 请求参数构建

```python
def _build_chat_kwargs(model_key, messages, temperature, max_tokens, tools, *,
                       stream, response_format=None) -> dict[str, Any]:
```

构建传给 `chat.completions.create()` 的请求参数：`model`（经
`ClientManager.get_model(model_key)`）+ messages + temperature + max_tokens + stream；
`tools` / `response_format` 可选追加；流式追加 `stream_options={"include_usage": True}`
（要求末尾 chunk 携带 usage，供结算退差）。

### _build_fallback_fn — fallback 降级函数

```python
def _build_fallback_fn(kwargs, model_key) -> Callable | None:
```

`LLMService._fallback_model_id` 为空返回 None（不启用）；否则返回闭包：`dict(kwargs)` 仅
替换 `model` 为备用模型，用 `ClientManager.get_client(model_key)` 发请求。**同 provider
约束**：复用主 client 的 base_url / 密钥（见「核心概念·fallback 同 provider」）。

### _build_event_fields — 事件字段构建

```python
def _build_event_fields(model_key, messages, temperature, has_tools, *, stream) -> dict:
```

构建 `llm_call` 事件字段（敏感信息脱敏，只记元数据：model / messages_count /
temperature / has_tools / stream）。返回可变 dict，调用点按结果逐步填充
success / error / duration / tokens（经 `fill_llm_event_fields` 落盘）。

### _rate_limited_call — 限流闭环

每次真实请求的**限流闭环**（见「核心概念·限流闭环」）：`reserve`（或 `reserve_adaptive`）
预留配额 → `create` → 失败/取消 `cancel()` 全额退并 re-raise。`active["res"]` 记录当前
reservation，供 create 成功后的 settle 读取（跨 create 与结算传递）。

### _count_prompt_tokens — TPM 估算

`_count_prompt_tokens(model_key, messages, max_tokens=0)`：TPM 桶扣减的估算量 =
prompt（每消息 +4 + content token + name +1，末尾 +2）+ `max_tokens` 输出余量。
`_get_encoder` 按模型解析 tiktoken 编码器（进程内缓存，未知模型回退 `cl100k_base`）；
`_content_to_text` 归一化多模态 content（见「核心概念·TPM 估算」）。

### LLMService 编排方法

| 方法 | 编排结构 |
| --- | --- |
| `async_generate` | 构建 kwargs → fallback → 准备 limiter（自适应/固定估算）→ 构造 `rectifier_context` → `rectified_stream`（整流循环）yield SSE 事件 |
| `generate` | 构建 kwargs → fallback → retry.execute（call_fn=限流闭环）→ `try/finally` 解析 + settle 结算 → 事件日志 |
| `generate_structured` | 委托 `StructuredOutput.extract`（三级降级，见 [structure.md](structure.md)） |

> 关键逻辑示意，完整实现见 `llm_service.py`。

---

## 执行流程

### async_generate（流式全链路）

```text
async_generate(messages, tools, temperature, max_tokens, result, model_key, cancel_event)
  ├─ _build_chat_kwargs(stream=True) + _build_fallback_fn
  ├─ 估算：adaptive → prompt_tokens；否则 estimated = prompt + max_tokens
  ├─ limiter = ReservationLimiterManager.get(model_key)
  └─ rectified_stream（整流循环，见 streaming_rectifier.md）：
       每 attempt：_rate_limited_call（reserve + create，经 retry.execute 保护）
         ├─ create 失败/取消 → cancel() 全额退 → 可整流则重试，否则放弃
         ├─ 迭代：_apply_chunk 累积 StreamResult + 产出 SSE 事件
         ├─ 中断：_should_rectify？ 是 → 退避重试（重新 reserve）；否 → 熔断 feeding + 中断收尾
         └─ 成功读完 / 硬取消 → settle(actual) / finally settle(None) 保留配额
```

### generate（非流式全链路）

```text
generate(messages, tools, temperature=0, max_tokens=1024, response_format, model_key="fast")
  ├─ _build_chat_kwargs(stream=False, response_format?) + _build_fallback_fn
  ├─ 估算（同 async_generate）+ limiter
  ├─ retry.execute(call_fn=_rate_limited_call, fallback_fn)
  │    ├─ 可恢复错误（超时/5xx/429）重试耗尽 → fill 事件(error) → 返回 None
  │    └─ 不可恢复错误（NON_RETRYABLE）→ fill 事件(error) → raise
  ├─ try: StreamParser.parse_non_stream(response) → 填 StreamResult
  └─ finally: active.res → settle(usage.total_tokens)；settle 被取消 → settle(None) 兜底 + re-raise
  └─ fill_llm_event_fields(success=True, usage, finish_reason) → 返回 StreamResult
```

### generate_structured（委托三级降级）

```text
generate_structured(messages, schema, model_key="fast", max_tokens=None)
  └─ StructuredOutput.extract(llm_service=self, ...)   # 三级降级见 structure.md
       第一级 JSON Schema(strict) → 第二级 JSON Mode → 第三级 正则提取
       截断短路返回 None；拒答/工具调用抛异常
```

---

## 对外接口

> 对外接口契约（`LLMService` 方法表 / 参数 / 返回 / 异常语义 / 调用示例）见
> [llm.md](llm.md)——模块对外接口文档为唯一事实源，本文档不重复。

对外依赖面即 `LLMService` 公共方法：`async_generate` / `generate` /
`generate_structured` / `calculate_cost` / `register_config` / `__init__`。内部辅助函数
（`_build_chat_kwargs` / `_build_fallback_fn` / `_build_event_fields` /
`_rate_limited_call` / `_count_prompt_tokens`）为私有实现，不构成对外接口。

---

## 边界情况

1. **硬取消保留配额（LLM-003）**：流式迭代 `finally` 由 `cancel()`（全额退含 RPM）改为
   `settle(None)`（保留配额 + 标记终态）——已发出请求不可回滚，防客户端配额虚增 → 429
2. **限流中途取消**：`_rate_limited_call` 的 `except BaseException`（含 CancelledError）
   → `cancel()` 全额退（请求未发出），re-raise 不泄漏预留
3. **settle 被取消兜底（LLM-002）**：`generate` 解析阶段 `finally` 内 `settle` 被硬取消 →
   未终态 res `settle(None)` 收尾 + re-raise（不吞取消信号）
4. **解析异常结算**：`parse_non_stream` 抛异常 → `sr.usage` 为 None → `settle(None)`
   保留全部预留 + 标记终态（闭环不泄漏）
5. **可恢复 vs 不可恢复错误**：`generate` 对可恢复（超时/5xx/429）重试耗尽返回 None
   （调用方按「业务无结果」）；不可恢复（4xx/认证/熔断开启）`classify_error ==
   NON_RETRYABLE` → 上抛让调用方感知
6. **fallback 同 provider 约束（LLM-012）**：跨 provider 配置 fallback → 400/404 静默
   失效；fallback 成败不进入熔断状态机（纯兜底）
7. **多模态 content 估算**：content 为 list（多模态）只取文本片段参与 token 估算，
   图片等非文本条目不编码；`content=None` → 空串（不抛 TypeError）
8. **整流重试配额（LLM-034）**：整流每轮重新 reserve + create（新请求语义）；fallback
   不参与 reserve（独立于主模型配额）

---

## 配置项清单

`LLMService` 运行期配置（`register_config` 注入，装配根 `container.initialize()` 读
settings 后调用）：

| 配置 | 类型 | 说明 |
| --- | --- | --- |
| `fallback_model_id` | str | 降级备用模型（须同 provider；空 = 不启用） |
| `adaptive_reserve` | bool | 自适应预留开关（高分位估算输出，减少占桶；默认关） |
| `stream_max_retries` | int | 流式整流重试次数（首 token 前中断才整流） |

> 其余配置（模型 / 重试 / 熔断 / 限流 / 整流 / 结构化）由各组件 `register_config`
> 注入，见各组件子文档「配置项清单」。

---

## 测试状态

- `tests/unit/test_llm_service.py`（5 用例，直接覆盖）：fallback 传递 / `content=None`
  估算 / 多模态 list 估算 / 解析错误 settle 结算 / settle 被取消兜底结算
- 间接覆盖（经 Facade 全链路）：`test_stream_rectify.py`（22 用例，async_generate 整流 /
  结算 / 事件 / 熔断 feeding）、`test_generate_structured.py`（49 用例，generate_structured
  三级降级）

---

## 设计决策

> 编排相关设计决策已归档至对应 ADR（Context → Decision → Consequences），此处仅列
> 与本模块直接相关的决策链接：

- 流式整流重试（整流循环与限流/结算协作）：[LLM-ADR-005](../../../adr/integration/llm/2026-08-01-streaming-rectification-retry.md)
- 限流算法与结算语义（reserve/settle）：[LLM-ADR-008](../../../adr/integration/llm/2026-08-01-rate-limit-token-bucket-waiting.md) · [LLM-ADR-009](../../../adr/integration/llm/2026-08-02-reserve-settle-semantics.md)
- 连接池管理：[LLM-ADR-004](../../../adr/integration/llm/2026-08-01-client-pool-lazy-close-tracking.md)

---

## 问题记录

> 涉及 llm_service 编排的问题已提取归档，完整生命周期（发现 → 分析 → 修复 → 验证 →
> 教训）见：

- [流式失败信号透传（LLM-001）](../../../issues/integration/llm/2026-08-16-stream-error-propagation.md)
- [非流式配额结算兜底（LLM-002）](../../../issues/integration/llm/2026-08-16-generate-quota-settle-fallback.md)
- [流式硬取消保留配额（LLM-003）](../../../issues/integration/llm/2026-08-16-hard-cancel-rpm-refund.md)
- [fallback 同 provider 约束（LLM-012）](../../../issues/integration/llm/2026-08-16-fallback-same-provider.md)
- [配额缺口：重试/降级不计入限流申请（LLM-034）](../../../issues/integration/llm/2026-08-02-quota-gap-retry-degradation-not-limited.md)
- [generate_structured 参数名契约（LLM-036）](../../../issues/integration/llm/2026-08-16-generate-structured-model-key-param.md)

---

## 相关文档

- [llm.md](llm.md)（模块对外接口文档：LLMService 契约 / 内部组件导航）
- [streaming_rectifier.md](streaming_rectifier.md)（整流循环 / 结算闭环）
- [limiter.md](limiter.md)（reserve/settle 限流语义）
- [retry.md](retry.md)（create 阶段重试/熔断/fallback）
- [streaming.md](streaming.md)（`parse_non_stream` 非流式解析）
- [structure.md](structure.md)（结构化输出三级降级）
- [cost_tracker.md](cost_tracker.md)（成本计算）
- [集成层说明](../README.md)（层总览）
