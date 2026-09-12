# LLMService 编排设计文档

> **模块**：`app/integration/llm/llm_service.py`
> **更新日期**：2026-09-10
> **职责**：LLM 网关统一 Facade——组织 11 个内部组件协作完成一次 LLM 调用（可靠性链 +
> 配额结算闭环 + 事件日志）
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
> **定位**：对外接口契约见 [llm.md](llm.md)（模块对外接口文档）；本文档解释
> `LLMService` **内部如何组织组件工作**（编排机制，供内部维护者 / 集成方）
> **配套**：实现领域端口 `LLMGateway`；依赖 `ClientManager` / `RetryHandler` /
> `StreamingRectifier` / `StreamParser` / `ReservationLimiter` / `StructuredOutput` /
> `RequestBudgetManager` / `execution_control` / `CostTracker` / `errors`（下游决策与执行终止翻译）；复用 `token_counter` 的
> `get_encoder` / `content_to_text` / `TiktokenTokenCounter`（tiktoken 计数组件）

---

## 📋 目录

- [LLMService 编排设计文档](#llmservice-编排设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [可靠性链（每次调用）](#可靠性链每次调用)
    - [真实请求入口（\_budget\_guarded\_call）](#真实请求入口_budget_guarded_call)
    - [结算闭环（finally 兜底）](#结算闭环finally-兜底)
    - [整流 × 限流协作](#整流--限流协作)
    - [fallback 同 provider](#fallback-同-provider)
    - [TPM 估算](#tpm-估算)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [\_CallContext — 已就绪请求上下文（主/fallback/续接共享）](#_callcontext--已就绪请求上下文主fallback续接共享)
    - [\_build\_chat\_kwargs — 请求参数构建](#_build_chat_kwargs--请求参数构建)
    - [\_build\_event\_fields — 事件字段构建](#_build_event_fields--事件字段构建)
    - [\_budget\_guarded\_call — 真实请求入口](#_budget_guarded_call--真实请求入口)
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
   入口，调用方不直接触碰 11 个内部组件；内部组织组件协作的细节对调用方透明
2. **可靠性链闭环**：限流（事前排队）→ 重试/熔断/降级（保护 create 阶段）→ 整流/续接
   （流式）→ 解析 → 事件日志，一次调用走完整链路
3. **配额结算闭环**：每个 `reserve` 必有结算或补偿责任；按 create 调度阶段、终止类型与已获响应选择 `cancel()`、`settle(actual)` 或 `settle(None)`，不能将所有 create 异常都全额退款（见「真实请求入口」阶段表）
4. **流式整流/续接与限流协作**：整流与半流续接的每轮 attempt 都重新进入 call_fn =
   重新 `reserve` + `create`（新请求语义，见 [LLM-034](../../../issues/integration/llm/2026-08-02-quota-gap-retry-degradation-not-limited.md)；续接另见 [LLM-ADR-015](../../../adr/integration/llm/2026-09-03-mid-stream-continuation.md)）

---

## 核心概念解释

### 可靠性链（每次调用）

一次 LLM 调用依次经过（各组件设计见对应子文档）：

```text
execution_control（取消/deadline 快检与受控等待）
    → RequestBudgetManager（最终 provider 请求上下文准入）
    → ReservationLimiter（事前限流：reserve 排队，配额不足等待而非请求）
    → RetryHandler（重试/熔断/fallback：保护 create 阶段，NON_RETRYABLE 上抛）
    → StreamingRectifier（流式：整流循环） 或  StreamParser（非流式：parse_non_stream）
    → fill_llm_event_fields（llm_call 事件日志：model/tokens/duration/success）
```

限流是**事前**（proactive），重试是**事后**（reactive），两者互补：客户端限流减少触发
服务端 429，真遇到 429 由重试层尊重 `Retry-After` 兜底。

### 真实请求入口（_budget_guarded_call）

每次真实主模型请求（整流 attempt / 半流续接 / 非流式重试）都经**单一入口** `_budget_guarded_call`。
入口的共享请求件（client / 预留策略 / 结算容器 `active` / 取消信号）由 `_CallContext` 承载
（一次调用内主/副/续接共享），`budget_guard` / `limiter` 按 guard_key 由调用点解析传入。
内部两重准入按序执行、职责分明：

```text
_budget_guarded_call
  ├─ ① 请求预算闸 RequestBudgetManager.validate   先于 reserve：超限请求不预留配额、不触网络
  ├─ ② 限流闭环 reserve：主/副/整流/续接每次真实请求重新 reserve（fallback 用独立池）
  ├─ ②.5 预留后取消复查 cancel_event：命中 → cancel 退款，不发起 SDK 请求（覆盖取消竞态）
  └─ ③ create 调度 → 按调度状态、终止类型与已获响应接管退款/结算/关流（见下表）
```

预算校验是入口的**独立第一步**，不混入限流步骤内部；超限（`ContextWindowExceededError`）
在网络调用前上抛，由整流器/领域层终结（见 [request_budget.md](request_budget.md)）。
fallback 备用链路与主请求共享同一请求生命周期（fallback 键窗口 + 独立配额池），见「fallback 同 provider」。

**阶段说明（非生产代码复制）**：执行控制围绕入口、reserve 返回后、create 调度及响应接管建立检查点。`create_started` 用于区分“尚未启动副作用”与“请求可能已到 provider”；受控等待返回迟回值时仍需由调用方接管资源并复查终止。

| 出口 / 已知事实 | 责任与行为 |
| --- | --- |
| 预算准入拒绝，尚未 reserve | 直接抛预算错误，不申请配额或调用 provider |
| reserve 内部已部分扣减但尚未移交 Reservation | reserve 本层负责补偿已扣条目，完成必要清理后传播终止 |
| 已得 Reservation，create 尚未启动即取消/到期 | `cancel()` 全额退 RPM/TPM；不发起 create |
| create 已调度后业务取消、期限或外层硬取消，未获得可用响应 | `settle(None)` 保守保留预留并标记终态；请求可能已到 provider，不声明远端未执行 |
| create 自然传输失败 | 维持当前项目的 `cancel()` 退款策略并传播错误；本地退款不证明供应商实际费用为零 |
| create 成功，或吞取消后以响应/流迟回 | 调用方接管返回值；有实际 usage 则 `settle(actual)`，无则 `settle(None)`；未读完的流由责任方关闭 |
| 迟回结果接管后仍命中终止 | 保留已获 usage，按公开契约终止，不将迟回值作为业务成功继续执行 |

`settle(actual)` 只退未用 TPM 差，RPM 不退。以上阶段契约由 [LLM-044](../../../issues/integration/llm/2026-09-08-execution-control-through-every-call.md) 与 [LLM-045](../../../issues/integration/llm/2026-09-09-execution-control-late-result-drop.md) 说明；不能用宽泛的 `except BaseException` 全退伪代码取代这些分支。

### 结算闭环（finally 兜底）

| 通道 | 兜底 |
| --- | --- |
| 流式（`async_generate`） | `rectified_stream` 迭代 `finally`：create 成功后的中断/取消统一 `settle(actual)`；**硬取消 `settle(None)` 保留配额 + 标记终态**（[LLM-003](../../../issues/integration/llm/2026-08-16-hard-cancel-rpm-refund.md)） |
| 非流式（`generate`） | `try/finally` 解析 + 结算：解析抛异常 → `settle(None)` 保留；`settle` 被硬取消 → 未终态 res `settle(None)` 兜底 + re-raise（[LLM-002](../../../issues/integration/llm/2026-08-16-generate-quota-settle-fallback.md)） |

**统一原则**：终止不能撤销可能已经到达 provider 的请求。create 已调度后的业务终止保守结算，尚未启动的预留可以补偿；自然传输失败按上方独立分支处理。不能把收到取消、没有成功响应或本地退款解释为远端副作用已回滚。

### 整流 × 限流协作

`async_generate` 的整流循环（`StreamingRectifier.rectified_stream`）每次 attempt 重新调用
`create_fn`（即 `_budget_guarded_call`，预算准入 → 限流闭环）——重新 `reserve` + `create`。
整流重试每轮都是**新请求**，重新扣配额（测试断言整流 2 轮 `calls["reserve"] == 2`）。
半流续接（[LLM-ADR-015](../../../adr/integration/llm/2026-09-03-mid-stream-continuation.md)）
的续接 attempt 同样经 `_budget_guarded_call` 重新校验预算 + `reserve` + `create`——但为
**尽力而为单链**（不经 `retry.execute`/fallback），续接失败退化放弃。

fallback（备用模型）**参与 reserve/settle**：fallback 闭包在 `_plan_request` 内联构造
（与主 `call_fn` 同构），走与主请求相同的 `_budget_guarded_call` 闭环，但用 fallback
**独立配额池**（fallback 键），Reservation 写入本次请求链同一 `active` 由调用方统一结算（成功 settle 一次、
流中断按已获 usage 或 None 结算，不双结算）。`active` 是当前请求链的结算责任位置，不是全局并发上限。

### fallback 同 provider

fallback 闭包（`_plan_request` 内构造，guard_key = `"fallback"`）复用主调用构建的 `kwargs`，
仅覆盖 `model` 为备用模型——fallback 用主模型 client（同 base_url / 密钥）发请求，**只支持同
服务商便宜模型降级**（如 deepseek-chat → deepseek-reasoner）。配置跨 provider 模型会打到主端点带备用
模型名 → 400/404，fallback 静默失效（[LLM-012](../../../issues/integration/llm/2026-08-16-fallback-same-provider.md)）。

**约束边界**：LLM-012 只约束 base_url / 密钥复用；备用模型的上下文窗口与配额是模型实体
属性，走独立 `fallback` 键配置（`LLM_FALLBACK_CONTEXT_WINDOW_TOKENS` / RPM / TPM），
**不沿用主 model_key 窗口、不共享主配额池**——同端点 ≠ 同窗口。窗口校验与限流都在每次
fallback 真实请求的 `_budget_guarded_call` 内完成，预算拒绝（`ContextWindowExceededError`）
在 retry 层直抛、不包成主网络故障 cause（否则会被下游当可恢复错误触发再调主）。

### TPM 估算

TPM 预留量在 `_plan_request` 内估算：计数委托 `TiktokenTokenCounter.count_messages_tokens`
（口径：每消息 +4 + content token + name +1，末尾 +2；编码器按模型进程内缓存、未知模型
回退 `cl100k_base`，见 [token_counter.md](token_counter.md)）。

两种预留形态（按 `llm_adaptive_reserve`）：

- 自适应：`prompt_tokens` = 计数（`max_tokens` 分传 `reserve_adaptive`，输出余量不并入）；
- 固定形态：`estimated` = 计数 + `max_tokens` 输出余量——TPM 桶按「请求可能消耗的最大
  token」扣减，宁可高估不错放。

`content` 归一化（None → 空串；多模态 list → 只取文本片段拼接）由 `count_messages_tokens`
内部完成，图片等非文本条目不参与估算（避免 `encode(None)` 抛 TypeError）。

---

## 架构总览

```text
外部调用方（ReActAgent / 应用层 / API 层）
        │  经 LLMGateway 端口
        ▼
    LLMService（Facade 编排）
      ├── async_generate（流式）→ StreamingRectifier.rectified_stream（整流循环）
      │        └─ create_fn = _budget_guarded_call（预算准入 → 限流闭环 reserve → create → cancel）
      │             └─ 每次 attempt 重新预算校验 + reserve（新请求语义）
      ├── generate（非流式）→ retry.execute（重试/熔断/fallback）
      │        └─ call_fn = _budget_guarded_call（预算准入 → 限流闭环）
      │        └─ try/finally：StreamParser.parse_non_stream + settle 结算
      ├── generate_structured → StructuredOutput.extract（三级降级，见 structure.md）
      └── calculate_cost → CostTracker.calculate
```

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 编排层 | `LLMService` | 组织各组件协作（方法分派 / 闭环控制 / 事件日志） |
| 真实请求层 | `_budget_guarded_call` | 单一入口：预算准入（先于 reserve）→ 限流闭环（reserve → create → cancel 兜底） |
| 可靠性层 | `RetryHandler`（经 `RetryHandlerManager.get`） | 保护 create 阶段：重试/熔断/fallback |
| 整流层 | `StreamingRectifier`（经 `rectified_stream`） | 流式整流循环（首 token 前中断重试） |
| 数据层 | `StreamParser`（经 `parse_non_stream`） | 非流式完整响应解析 |
| 结构化层 | `StructuredOutput`（经 `extract`） | 结构化输出三级降级 |
| 成本层 | `CostTracker`（经 `calculate`） | 按模型用量估算成本 |

---

## 组件详解

### _CallContext — 已就绪请求上下文（主/fallback/续接共享）

```python
@dataclass(frozen=True)
class _CallContext:
    client: AsyncOpenAI
    active: dict[str, Reservation]
    adaptive: bool
    prompt_tokens: int
    estimated: int
    max_tokens: int
    cancel_event: asyncio.Event | None = None
```

一次 LLM 调用内「真实请求前后恒定」的请求件在此一次性装配：client（连接池缓存复用）、
限流预留策略（adaptive + token 估算，整流 / 重试循环外一次算好）、跨闭包共享的结算容器
`active` 与业务取消信号；由 `LLMService._plan_request` 构造后供其内联闭包消费。
`budget_guard` / `limiter` 按 guard_key 各异（fallback 独立键
窗口 + 独立池），**不进 ctx**——由各闭包在真实请求时经 Manager 解析，保持每次真实调用重新
reserve。`active` 为可变 dict（frozen 只防字段被替换）。

### _build_chat_kwargs — 请求参数构建

```python
def _build_chat_kwargs(model_key, messages, temperature, max_tokens, tools, *,
                       stream, response_format=None) -> dict[str, Any]:
```

构建传给 `chat.completions.create()` 的请求参数：`model`（经
`ClientManager.get_model(model_key)`）+ messages + temperature + max_tokens + stream；
`tools` / `response_format` 可选追加；流式追加 `stream_options={"include_usage": True}`
（要求末尾 chunk 携带 usage，供结算退差）。由 `LLMService._plan_request` 统一调用——
方法原始参数在此归一为 provider 请求 kwargs（channel 专属 stream / response_format 入参）。

### _build_event_fields — 事件字段构建

```python
def _build_event_fields(model_key, messages, temperature, has_tools, *, stream) -> dict:
```

构建 `llm_call` 事件字段（敏感信息脱敏，只记元数据：model / messages_count /
temperature / has_tools / stream）。返回可变 dict，调用点按结果逐步填充
success / error / duration / tokens（经 `fill_llm_event_fields` 落盘）。

### _budget_guarded_call — 真实请求入口

每次真实请求的**单一入口**（见「核心概念·真实请求入口」），共享请求件经 `_CallContext` 传入
（`budget_guard` / `limiter` 由调用点按 guard_key 解析），按序两段：① 请求预算闸校验
（`reserve` 之前，超限抛 `ContextWindowExceededError`、不占配额）；② 执行控制约束下的
`reserve`（或 `reserve_adaptive`）→ reserve 后复查 → 受控 `create`。create 前终止全额
退款；create 调度后取消/期限/硬取消以 `settle(None)` 保守结算；自然传输失败才 `cancel()`。
`ctx.active["res"]` 记录当前 reservation，供 create 成功后的 settle 读取（跨 create 与结算传递）。

### LLMService 编排方法

| 方法 | 编排结构 |
| --- | --- |
| `async_generate` | `_plan_request`（build kwargs + client/retry/配额估算/active/主副/续接闭包一次就绪）→ 构造 `rectifier_context` → `rectified_stream`（整流循环）yield SSE 事件 |
| `generate` | `_plan_request`（build kwargs + 同上前奏，无续接闭包）→ retry.execute（call_fn=限流闭环）→ `try/finally` 解析 + settle 结算 → 事件日志 |
| `generate_structured` | 委托 `StructuredOutput.extract`（三级降级，见 [structure.md](structure.md)） |

> 关键逻辑示意，完整实现见 `llm_service.py`。

---

## 执行流程

### async_generate（流式全链路）

```text
async_generate(messages, tools, temperature, max_tokens, result, model_key, cancel_event, deadline)
  └─ _plan_request（流式/非流式共用编排）：
       ├─ _build_chat_kwargs 组装 provider 请求 kwargs（stream=True 补 include_usage）
       ├─ client / retry 解析 + TPM 预留量估算（adaptive → prompt_tokens；否则 estimated = prompt + max_tokens）
       └─ ctx 就绪后内联构造 call_fn / fallback_fn（启用备用模型时 "fallback" 键）/ continue_fn（流式且配置开启时），
            各闭包按各自 guard_key 直接走 _budget_guarded_call 发起真实请求；产物见 _RequestPlan
  └─ rectified_stream（整流/续接循环，见 streaming_rectifier.md）：
        每 attempt：_budget_guarded_call（执行快检 → 预算 → 受控 reserve → 复查 → 受控 create，经 retry.execute 保护）
          ├─ create 自然失败 → cancel()；执行终止/硬取消 → settle(None)
          ├─ 迭代：_drain 竞争 chunk/cancel/deadline/idle + 累积 StreamResult + 产出 SSE 事件
         ├─ 中断：_should_rectify？ 是（首 token 前）→ 退避重试（重新 reserve）
         │                    否（已产出 content）→ 续接？ 是 → continue_fn(prefix) 续写
         │                                             否 / 续接失败 → 放弃（熔断 feeding + 部分保留）
         └─ 成功读完 / 硬取消 → settle(actual) / finally settle(None) 保留配额
```

### generate（非流式全链路）

```text
generate(messages, tools, temperature=0, max_tokens=1024, response_format, model_key="fast", cancel_event=None, deadline=None)
  ├─ _plan_request（同 async_generate 共用编排，无续接闭包）：build kwargs → 估算 + active + call_fn / fallback_fn 一次就绪
  ├─ retry.execute(call_fn=call_fn, fallback_fn=fallback_fn)
  │    ├─ 可恢复错误（超时/5xx/429）重试耗尽 → fill 事件(error) → 返回 None
  │    └─ 不可恢复错误（NON_RETRYABLE）→ fill 事件(error) → 统一决策（见 [error.md](error.md)）：openai 归一 LLMAPIError（from 原异常）；其余 raise
  ├─ try: StreamParser.parse_non_stream(response) → 填 StreamResult
  └─ finally: active.res → settle(usage.total_tokens)；settle 被取消 → settle(None) 兜底 + re-raise
  ├─ 结算后返回前复查 cancel/deadline；命中时携 usage 抛 shared 类型化终止异常
  └─ fill_llm_event_fields(success=True, usage, finish_reason) → 返回 StreamResult
```

### generate_structured（委托三级降级）

```text
generate_structured(messages, schema, model_key="fast", max_tokens=None, usage=None, cancel_event=None, deadline=None)
  └─ StructuredOutput.extract(llm_service=self, ...)   # 三级降级见 structure.md
       第一级 JSON Schema(strict) → 第二级 JSON Mode → 第三级 正则提取
       截断短路返回 None；拒答/工具调用抛异常
       cancel_event/deadline：每条子调用透传到 reserve/create/retry；命中类型化短路，extract 最外层返回 None
```

---

## 对外接口

> 对外接口契约（`LLMService` 方法表 / 参数 / 返回 / 异常语义 / 调用示例）见
> [llm.md](llm.md)——模块对外接口文档为唯一事实源，本文档不重复。

对外依赖面即 `LLMService` 公共方法：`async_generate` / `generate` /
`generate_structured` / `calculate_cost` / `register_config` / `__init__`。内部辅助函数
（`_build_chat_kwargs` / `_build_event_fields` / `_budget_guarded_call`）与编排私有件
（`_CallContext` / `_RequestPlan` / `LLMService._plan_request`）为私有实现，不构成对外接口。

---

## 边界情况

1. **硬取消保留配额（LLM-003）**：流式迭代 `finally` 由 `cancel()`（全额退含 RPM）改为
   `settle(None)`（保留配额 + 标记终态）——已发出请求不可回滚，防客户端配额虚增 → 429
2. **限流中途取消**：`_budget_guarded_call` 限流段的 `except BaseException`（含
   CancelledError）→ `cancel()` 全额退（请求未发出），re-raise 不泄漏预留
3. **settle 被取消兜底（LLM-002）**：`generate` 解析阶段 `finally` 内 `settle` 被硬取消 →
   未终态 res `settle(None)` 收尾 + re-raise（不吞取消信号）
4. **解析异常结算**：`parse_non_stream` 抛异常 → `sr.usage` 为 None → `settle(None)`
   保留全部预留 + 标记终态（闭环不泄漏）
5. **可恢复 vs 不可恢复错误**：`generate` 对可恢复（超时/5xx/429）重试耗尽返回 None
   （调用方按「业务无结果」）；不可恢复（4xx/认证/熔断开启）统一决策（`decide_downstream_error`，
   见 [error.md](error.md)）→ 上抛让调用方感知。其中 openai `APIStatusError` 系列（4xx/认证/
   响应校验）归一为 `LLMAPIError`（`AppError` 树，`raise ... from e` 保留原始异常，
   status_code 保留供 structured 的 response_format 400 降级判定）——领域层
   `except AppError` 可统一兜底集成层透出的不可恢复错误（REASON-010 闭环）；
   非 openai 异常（`CircuitBreakerOpenError`/编程错误）原样透传
6. **fallback 同 provider 约束（LLM-012）**：跨 provider 配置 fallback → 400/404 静默
   失效；fallback 成败不进入熔断状态机（纯兜底）。窗口与配额按独立 `fallback` 键配置，
   **不沿用主键窗口、不共享主配额池**（同端点 ≠ 同窗口）
7. **多模态 content 估算**：content 为 list（多模态）只取文本片段参与 token 估算，
   图片等非文本条目不编码；`content=None` → 空串（不抛 TypeError）
8. **整流重试配额（LLM-034）**：整流每轮重新 reserve + create（新请求语义）；fallback
   经同一闭环但用 fallback 独立配额池（与主链路共 `active`，结算单次、不双退）

---

## 配置项清单


配置键的完整定义与默认值见 [配置参考](../../config_doc/config.md)；本节仅记录与本组件相关的行为。

`LLMService` 运行期配置（`register_config` 注入，装配根 `container.initialize()` 读
settings 后调用）：

| 配置 | 类型 | 说明 |
| --- | --- | --- |
| `fallback_model_id` | str | 降级备用模型（须同 provider；空 = 不启用） |
| `adaptive_reserve` | bool | 自适应预留开关（高分位估算输出，减少占桶；默认关） |
| `stream_max_retries` | int | 流式整流重试次数（首 token 前中断才整流） |
| `continuation_max_retries` | int | 半流续接轮次上限（已产出 content 中断续写，LLM-ADR-015；默认 0=禁用，settings 默认 1） |

> 其余配置（模型 / 重试 / 熔断 / 限流 / 整流 / 结构化）由各组件 `register_config`
> 注入，见各组件子文档「配置项清单」。

---

## 测试状态

- `tests/unit/test_llm_service.py`（10 用例，直接覆盖）：fallback 传递 / `content=None`
  估算 / 多模态 list 估算 / 解析错误 settle 结算 / settle 被取消兜底结算 / 异常归一决策
  （401→`LLMAPIError`、响应校验归一、未知非 openai 原样上抛、可恢复→None）/ generate
  reasoning_content / has_reasoning 回填（LLM-040）
- 间接覆盖（经 Facade 全链路）：`test_stream_rectify.py`（23 用例，async_generate 整流/续接 /
  结算 / 事件 / 熔断 feeding，含续接请求追加 assistant 前缀消息 + 重新 reserve）、
  `test_generate_structured.py`（50 用例，generate_structured 三级降级）
- LLM-044 执行控制：`test_llm_request_budget.py` 与 `test_streaming_rectifier.py` 覆盖 reserve/create、
  retry/续接退避、chunk 竞态、流关闭和终止 usage。

---

## 设计决策

> 编排相关设计决策已归档至对应 ADR（Context → Decision → Consequences），此处仅列
> 与本模块直接相关的决策链接：

- 流式整流重试（整流循环与限流/结算协作）：[LLM-ADR-005](../../../adr/integration/llm/2026-08-01-streaming-rectification-retry.md)
- 半流中断接续（已产出 content 带前缀续写，编排层构造 `continue_fn`）：[LLM-ADR-015](../../../adr/integration/llm/2026-09-03-mid-stream-continuation.md)
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
- [执行控制贯穿每笔真实请求（LLM-044）](../../../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)
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
