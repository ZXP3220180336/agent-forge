# LLM 连接建立期异常全景与守护机制决策（分级超时 / 连接池上限 / 首包-空闲双阈值）

> **状态**：✅ 已采纳（守护总图为现状；分级超时、连接池 `limits`、首包-空闲双阈值均已实施 2026-09-03，无遗留升级路径）
> **决策日期**：2026-09-03
> **涉及模块**：`app/config/settings.py` / `app/container.py`（装配）· `app/integration/llm/client.py`（连接池 + http_client）· `streaming_rectifier.py`（双阈值看门狗）· `errors.py` / `retry.py` / `reservation_limiter.py`（守护）
> **关联文档**：[error.md](../../../docs/integration_doc/llm_doc/error.md) · [retry.md](../../../docs/integration_doc/llm_doc/retry.md) · [client.md](../../../docs/integration_doc/llm_doc/client.md) · [streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md) · [config.md](../../../docs/config_doc/config.md)

---

## Context

- 生产级 Agent 流式链路「请求构建 → **连接建立** → 流式传输 → 业务消费 → 收尾」五阶段。**连接建立期** = 首次真实 HTTP 请求发出 → `create()` 返回流对象（响应头）之间：含池取连接、TCP/TLS 握手、等待响应头；其后首 chunk 起归**流式传输期**（整流接管）。
- 本仓库连接期守护分散于 client（连接池）/ errors（分类）/ retry（重试熔断 fallback）/ limiter（限流），各有 ADR（LLM-ADR-004/005/006/007/008/009/010/011/013）但**缺一张「应然异常 → 守护归属」总图**。
- 相对生产框架三缺口：**分级超时**（connect/pool/read 分档）、**连接池上限显式配置**、**首包（宽）与 chunk 空闲（窄）区分**——httpx read 档只能给单一值，双阈值须应用层逐 chunk 计时。审计另发现装配根此前**未把任何 timeout 传入 `ClientManager.register_config`**（openai 客户端实际用 SDK 默认超时），`llm_timeout` 是未被接线的遗留。
- **决策**：三缺口一次到位实施，不设升级路径（本决策覆盖连接期全部守护项）。
- **工业级参照**：OpenAI SDK 将 401/400/403 归永久错误不重试、网络/超时/5xx/429 归可重试（[OpenAI SDK 错误处理](https://deepwiki.com/openai/openai-python/3.4-error-handling-and-retry-logic)）；LangGraph 对 401 不重试、编程错误 fail-fast（[LangGraph Fault Tolerance](https://langchain-5e9cc07a.mintlify.app/oss/javascript/langgraph/fault-tolerance)）；httpx 分级超时 connect/read/write/pool + `Limits`（[httpx](https://www.python-httpx.org/advanced/timeouts/)）；生产流式框架主张连接/池/首包分档（[见流式链路审计](../../../issues/integration/llm/README.md)）。

---

## Decision

1. **阶段边界与守护归属总图**（采纳为规范锚点）：

```text
[调用方] async_generate / generate
   │  model_key 解析（装配期配置，非运行期故障）
   ▼
[1] ReservationLimiter.reserve      客户端限流（RPM/TPM 预占，请求前；LLM-ADR-008/009/010）
   ▼
[2] ClientManager.get_client        连接池复用 + 分级超时 + pool limits（httpx.Timeout/Limits）
   ▼
[3] RetryHandler.execute            create 阶段：有限重试 + 熔断 + fallback（LLM-ADR-006/007）
   │    ├─ classify_error 定可重试性（RETRYABLE / RATE_LIMITED / NON_RETRYABLE）
   │    ├─ 失败 attempt 逐次 re-reserve → cancel 全额退
   │    └─ 成功 → 流对象
   ▼
[4] StreamingRectifier             首 chunk 起整流 + 首包/空闲双阈值看门狗
```

   连接期异常**不向领域扩散**：流式经 `StreamResult.error` + error 事件外显；非流式由 `decide_downstream_error` 归一 `LLMAPIError` / 降级 None（LLM-ADR-013）。

1. **应然异常 × 守护 × 行为矩阵**（采纳现状为规范）：

| 连接期异常 | SDK/httpx 形态 | 分类 | 守护与行为 |
| --- | --- | --- | --- |
| DNS / TCP / TLS / 连接中断 | `APIConnectionError` / `httpx.NetworkError` | RETRYABLE | 指数退避重试；耗尽记熔断；connect 档防悬挂 |
| 超时（连接/池/写/读） | `APITimeoutError` / `TimeoutError` / `httpx.TimeoutException` | RETRYABLE | 分级超时触发 → 重试；首包/空闲看门狗区分思考慢与断流 |
| 429 限流 | `RateLimitError` | RATE_LIMITED | 退避 + 尊重 Retry-After；**CLOSED 不喂熔断**（HALF_OPEN 下是过载信号计失败） |
| 5xx / 503 过载 | `APIStatusError(5xx)` | RETRYABLE | 重试耗尽 → 请求级记熔断 → fallback 备用模型 |
| 4xx / 鉴权 / 参数 | `APIStatusError(4xx)` | NON_RETRYABLE | 不重试；探针 `release_probe`；`generate` 归一 `LLMAPIError`；structured 400 特判降级 |
| 熔断开启 | `CircuitBreakerOpenError` | — | 有 fallback 纯兜底（不重试不进状态机）；无 fallback 原样上抛 |
| 配置/未注册 key | `ValueError` | — | 装配期错误，非运行期输入（不重试） |

1. **熔断语义采纳**（重申 LLM-ADR-006/007）：CLOSED 窗口错误率或全失败达最小样本 → OPEN（recovery 后 HALF_OPEN）；探针单次不重试；429 例外；fallback 成败不进状态机；计数粒度为请求级。

1. **客户端限流采纳**（重申 LLM-ADR-008/009/010）：每次真实请求重新 reserve；失败/取消 `cancel()` 全额退；成功 `settle(actual)` 退差（LLM-039 口径）；fallback 不参与 reserve。

1. **三项机制实施（2026-09-03，一步到位）**：

   - **分级超时**：settings 拆 `llm_timeout_connect=10` / `read=60` / `write=10` / `pool=10`，属性 `llm_client_timeout` 返回 **`httpx.Timeout` 实例**（httpx `TimeoutTypes` 不接受 dict，dict 会在客户端构造期抛 TypeError）；container 注入三模型 `register_config(timeout=...)`；**修复原 timeout 未接线遗留**。
   - **连接池上限**：settings `llm_pool_max_connections=100` / `llm_pool_max_keepalive_connections=20`；任一 pool 字段或代理存在即触发 `_build_http_client` 构建 `httpx.AsyncClient`（含 `httpx.Limits` + 分级超时）注入 openai `http_client`——pool limits 只能经 http_client 传递；未配置保持直连（不注入）。
   - **首包/空闲双阈值**：settings `llm_timeout_first_token=60` / `llm_timeout_chunk_idle=15`；`StreamingRectifier` 迭代改为逐 chunk `asyncio.wait_for(anext(stream), 阈值)`——**首 chunk 用宽阈值（覆盖模型思考），其后每 chunk 用窄阈值（判断流）**；超时 `TimeoutError` 走既有整流/放弃分支（首 token 前整流、已产出放弃防重复输出）；空串 `TimeoutError` 描述回退类型名（`result.error` 以非空为失败信号，react 短路依赖——防把「看门狗超时」误当「成功空回」）。

1. **可观测**：连接期失败经 `llm_call` 事件 `success=False` + error（截断口径）落盘（LLM-ADR-011）；指标（分阶段耗时/中断位置分布）仍为 observability 文档待规划占位，非本决策升级项。

---

## Consequences

- ✅ 连接期守护一次性齐全：分级超时（防悬挂/池等待堆积/请求体卡死/读兜底）+ 连接池上限 + 首包-空闲双阈值（区分思考慢 vs 断流）——无遗留升级路径。
- ✅ 总图锚点 + 跨组件闭环（client/errors/retry/limiter/rectifier）；修复「timeout 未接线」装配缺口（此前 openai 客户端用 SDK 默认超时）。
- ✅ 看门狗超时经整流语义收敛：首 token 前超时整流（不重复输出）、已产出后超时放弃（保留部分内容 + 非空 error）——不新增领域层协议。
- ⚠️ 双阈值看门狗只作用于**整流流式路径**（async_generate）；非流式 `generate` 单响应体无「逐 chunk 空闲」语义，仍由 httpx read 档兜底首字节等待。
- ⚠️ `wait_for` 取消 anext 依赖 openai 流的内部清理；整流 finally `settle` 闭环保留（配额不泄漏），连接释放随 openai client 生命周期（close_all）。
- ⚠️ 配置面扩展：settings 字段（.env.example / config.md / tests 同步），`llm_timeout` 单字段移除。
