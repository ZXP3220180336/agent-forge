# 配额缺口：重试/降级不计入限流申请，真实请求数远超放行量

> **状态**：✅ 已修复（2026-08-02）
> **优先级**：P0（限流核心语义违背——RPM/TPM 桶低估 + fallback 零限流保护）
> **来源**：2026-08-02 审核调研（用户疑问）· 2026-08-16 从 llm.md 提取归档
> **涉及模块**：`app/integration/llm/reservation_limiter.py` · `app/integration/llm/retry.py` · `app/integration/llm/llm_service.py`
> **关联文档**：[llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [limiter.md](../../../docs/integration_doc/llm_doc/limiter.md)

---

## 问题描述

### 现象

`async_generate()` / `generate()` 在**进入** `retry.execute()` 前 `reserve` 一次限流（RPM 桶 1 个 + TPM 桶 estimated_tokens 个）。放行后进入重试/熔断/降级阶段——若调用失败会重试、多次失败会降级到备用模型。**这些后续真实发出的 API 请求，都发生在限流申请之后**，实际请求数可能远超限流器放行的量。

### 影响

以默认配置（`llm_max_retries=2`，retry 循环最多 3 次 call_fn）为例：

| 路径 | 是否重新 reserve | 真实请求数 |
| --- | --- | --- |
| 原始调用 | ✅ reserve 了 | 1 |
| retry 内部重试（×2） | ❌ **不 reserve** | +2 |
| fallback 降级（备用模型） | ❌ **不 reserve** | +1 |

一次 `async_generate` 最多可发 4 次真实请求，但只申请了 1 次限流。具体影响：

- **RPM 桶低估**：实际放行速率 = `rpm × (1 + 重试率 + 降级率)`。下游越不稳定，放大越严重（失败率 50% 时实际请求约是 RPM 的 2 倍）。
- **TPM 桶低估**：重试时同样的 prompt 重新发送，token 消耗翻倍，但 TPM 桶只在第一次 reserve 扣过一次。
- **fallback 零限流保护**：备用模型（`llm_fallback_model_id`）有自己的配额，当前 fallback 完全不 reserve，**无任何限流**。
- **整流重试是例外**：首 token 前中断的整流每轮都重新 reserve，此路径无缺口。

### 根因

限流器在重试外层只 `reserve` 一次——重试（=新请求）与降级（=新请求）未重新穿过限流器，申请量 vs 实际请求量不一致。

---

## 工业级参照

1. **中间件链布局决定答案**：工业界把限流器和重试做成中间件链，**限流器在重试外层**——每次真实请求（含重试）都重新穿过限流器、消耗 token（如 Go 的 `tgcp`：`"Each request consumes 1 token"`，重试也计）。这是主流做法——**重试天然计入配额**。参考：[Rethinking HTTP API Rate Limiting（IEEE/arXiv）](https://ieeexplore.ieee.org/document/11366354)、[tgcp 中间件链](https://pkg.go.dev/github.com/yogirk/tgcp@v0.4.0/internal/core)
2. **IETF 留白**：RateLimit Header Fields 草案明确「规范不规定非 2xx 响应是否消耗配额」，留给服务端/客户端设计决策。无唯一正确答案，但工业实现主流倾向是**外层包裹**。[IETF 草案](https://datatracker.ietf.org/meeting/109/agenda/httpapi-drafts.pdf)
3. **重要澄清**：客户端限流的第一目的是**防突发**（别瞬间打爆服务端），不是精确记账到每一分服务端配额。服务端还有第二道闸（429 + `Retry-After`），重试请求即使客户端没 reserve，服务端可能再 429 兜底。**但当重试原因是 5xx/超时（下游故障）时，重试请求会真实打到服务端并消耗 token——客户端不 reserve 就是超额**。
4. **LLM 生态实践**：客户端 Token Bucket 限流器（如 [plsno429](https://github.com/appleparan/plsno429)）作为 **proactive** 手段在请求前限流；重试（**reactive**）走指数退避 + jitter + 尊重 `Retry-After`。两者是互补的两层，不是同一件事。参考：[OpenAI 429 官方指南](https://help.openai.com/en/articles/5955604-how-can-i-solve-429-too-many-requests-errors)

---

## 修复方案（含决策取舍）

**决策**：**reserve 移入 call_fn，但 fallback 不参与 reserve。**

- **重试计入配额**：retry.execute 内部每次重试都重新 reserve（重试=新请求，扣配额合理）。
- **fallback 不参与 reserve**：客户端限流防的是**主模型**的突发，备用模型（降级路径）无需限流保护，且独立于主模型配额。

**修复要点**：

1. `_rate_limited_call` 为模块级辅助函数（`llm_service.py`），接收 `adaptive`/`limiter`/`client`/`kwargs`/`active`/`estimated` 等参数——限流闭环被 async_generate 和 generate 共用，不再闭包内联。
2. `estimated` 在循环外计算一次（messages 在一次 `async_generate` 内不变），避免每次重试重复 tiktoken 计数。
3. **整流重试**：整流循环（`StreamingRectifier`）每轮重新 `execute`，`create_fn` 内部再次 reserve——「新请求」语义正确。
4. **代价**：重试前会先 reserve，等待与退避叠加，延迟可能增大；但语义正确（重试=新请求，扣配额合理）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/llm_service.py` | `_rate_limited_call` 模块级辅助函数；reserve 移入 call_fn，fallback 不参与 reserve | `test_stream_rectify.py`（见下） |

**测试**（`tests/unit/test_stream_rectify.py`）：

- `test_rate_limiter_acquire_before_each_attempt`：整流 2 轮，每轮 call_fn 都 reserve（`calls == 2`）。
- `test_rate_limiter_acquire_on_retry_inside_execute`：retry.execute 内部重试也 reserve（create 第 1 次抛 RETRYABLE → 重试第 2 次，`calls == 2`）。

---

## 验证

- 重试每轮重新 reserve（重试=新请求，扣配额合理）；fallback 不参与 reserve（独立于主模型配额）
- 整流每轮重新 reserve——「新请求」语义正确
- 全量测试通过（2026-08-02 修复时验证）

---

## 教训沉淀

- **限流与重试的布局决定配额语义**：限流器放行一次 ≠ 一次真实请求——重试是新的真实请求，必须重新穿过限流器（中间件链外层包裹）；否则失败率越高，实际请求越远超配额。
- **fallback 与主模型配额解耦**：备用模型独立于主模型配额，客户端限流只防主模型突发——降级路径不参与 reserve。
