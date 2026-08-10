# StreamingRectifier 设计文档

> **模块**：`app/services/llm/streaming_rectifier.py`
> **职责**：流式整流重试策略——「首 token 前中断 → 重新 create + 重新迭代」，已产出 token 后中断则放弃
> **定位**：从 `LLMService.async_generate` 拆出的独立策略类（无状态静态类，不实例化），让 Facade 保持编排职责
> **配套**：`StreamParser`（chunk 解析）、`LLMService.async_generate`（编排）、`RetryHandler`（create 阶段重试/熔断）

---

## 设计目标

1. **整流策略独立**：整流重试循环、`emitted_any` 状态、熔断 feeding 与 Facade 编排正交，独立成类
2. **首 token 前才整流**：用户没看到任何输出时重试，不产生重复内容
3. **已产出不整流**：避免重复输出 / token 双倍计费 / tool_calls 残缺
4. **结算闭环**：create 成功后的中断/取消统一 settle，硬取消 finally 兜底 cancel
5. **熔断观察**：迭代「放弃时」且异常 RETRYABLE → 喂熔断器，感知「create 正常但流频繁中断」

---

## 核心设计

### 为什么需要整流（问题背景）

`retry.execute()` 只保护 `client.chat.completions.create()`（创建响应对象），真正的 `async for chunk in response:` 迭代在重试范围外。流中途断掉（读超时 / 连接重置 / 解析失败）时，旧实现只捕获报错（记日志 + 错误事件），不重试。

### 整流条件（`_should_rectify`，全部满足才整流）

| 条件 | 说明 |
| --- | --- |
| 首 token 前（`emitted_any=False`） | `reasoning_token` / `message_token` / `tool_call_deltas` 任一非空即置位；`finish_reason` / `usage` **不算**"首 token"（纯 usage/finish 死流仍可整流） |
| 未超整流上限 | `attempt < stream_max_retries` |
| 异常可恢复 | `classify_error` ∈ `RETRYABLE` / `RATE_LIMITED`；NON_RETRYABLE（4xx/校验错误/截断/未知）不整流 |
| 用户未取消 | `cancel_event` 未置位 |

**整流重试**：重新 create + 重新迭代。**create 阶段异常绝不整流**——`retry.execute` 已决定重试/熔断/fallback，400 / `CircuitBreakerOpenError` / fallback 失败走既有错误路径。

### 结算闭环（reservation）

| 出口 | 处理 |
| --- | --- |
| 成功读完 | `settle(actual)` 退 TPM 差 |
| 迭代中断 / 用户取消 | `settle(actual)`（请求已发出，无论整流与否） |
| 硬取消（CancelledError） | `finally` 兜底 `cancel()` 全额退（R1：不泄漏预留） |

每次整流 attempt 由调用方的 `create_fn` 重新 reserve（新请求语义）。

### 熔断 feeding

流式迭代「放弃时」（不整流）且异常为 RETRYABLE → `retry.circuit_breaker.record_failure()`，让熔断器感知「create 正常但流频繁中途断开」的下游故障。整流成功不喂、NON_RETRYABLE / RATE_LIMITED / cancel 不喂。

### 事件日志

每次尝试独立计时，失败走 `success=False` + error，成功清 `error=None`（整流成功 = 1 条失败 + 1 条成功日志）。日志填充复用 `utils/logger.py` 的 `fill_llm_event_fields`（通用 LLM 事件日志工具）。

### 数据流

```
create_fn（限流闭环 reserve + create）
    → retry.execute() 保护 create 阶段
    → rectified_stream 整流循环
        ├─ _apply_chunk：解析 chunk → 累积 StreamResult + 产出事件
        ├─ _should_rectify：判断是否整流
        └─ _finish_interrupted：中断收尾（settle + 日志）
```

## 关键实现

### 配置注入（register_config）

流式整流退避配置由外层 `register_config()` 注入（AppState 读 settings 后调用），子模块不直接依赖 settings：

```python
StreamingRectifier.register_config(
    base_delay=settings.llm_base_delay,
    max_delay=settings.llm_max_delay,
    use_jitter=settings.llm_use_jitter,
)
```

退避公式与 create 阶段一致：`base_delay × 2^attempt`，上限 `max_delay`，可选随机抖动。**不新增独立退避配置**。

### 调用方式

`rectified_stream` 是无状态静态方法，产出 SSE 事件字符串（与 `async_generate` 的 yield 契约一致）：

```python
rectifier_context = RectifierContext(result, active, event_fields)
async for event in StreamingRectifier.rectified_stream(
    create_fn=lambda: _rate_limited_call(...),   # 每次整流 attempt 重新 reserve + create
    retry=retry,
    cancel_event=cancel_event,
    stream_max_retries=settings.llm_stream_max_retries,
    context=rectifier_context,                    # 会话共享状态
    fallback_fn=fallback_fn,
):
    yield event
```

### RectifierContext

整流会话共享的可变状态（跨 attempt 传递），由调用方构造并持有：

```python
@dataclass
class RectifierContext:
    result: StreamResult            # 累积输出
    active: dict[str, Reservation]  # 活跃 reservation（成功 settle / 失败 cancel）
    event_fields: dict[str, Any]    # 日志字段
```

## 配置项

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `llm_stream_max_retries` | int | `1` | 流式整流重试次数（首 token 前中断才整流；`0`=禁用） |
| `llm_base_delay` / `llm_max_delay` / `llm_use_jitter` | — | — | 整流退避（经 `register_config` 注入，与 create 阶段共用） |

`llm_stream_max_retries` 独立于 `llm_max_retries`：create 重试（HTTP 请求级）与整流重试（已开始流式后重启）属不同故障阶段，需独立调优。

## 工业级对照

| 来源 | 结论 |
| --- | --- |
| OpenAI Python SDK | `max_retries` 只覆盖初始 HTTP 请求，不重试 mid-stream——"用户已消费部分输出，mid-stream 重试语义不清晰" |
| LangChain `langchain-failover` | 只在主模型**产出第一个 token 前**死亡时 failover——"你永远不会得到重复的、半流输出" |
| awaken 运行时 | 4 级恢复（ContinueText / SynthesizeToolUse / TruncateBeforeTool / WholeRestart），WholeRestart（整流重试）只在无文本、无完整工具调用时用 |

## 测试

- `tests/unit/test_streaming_rectifier.py`（6 用例，直接覆盖整流策略）：首 token 前中断整流 / 已产出不整流 / cancel 不整流 / 整流上限耗尽 + 熔断 feeding / 成功 settle / 硬取消 finally cancel
- `tests/unit/test_stream_rectify.py`（21 用例，经 `LLMService.async_generate` 间接覆盖）：整流/结算/事件/日志/熔断 feeding 全链路断言

## 相关文档

- [LLM 层总览](llm.md)（async_generate 编排）
- [StreamParser](streaming.md)（chunk 解析，`ParsedChunk`）
- [重试与熔断](retry.md)（`RetryHandler` / `classify_error` / 熔断 feeding）
- [限流器](limiter.md)（reserve/settle 结算闭环）
- [全局日志框架](../../logging.md)（`fill_llm_event_fields`）
