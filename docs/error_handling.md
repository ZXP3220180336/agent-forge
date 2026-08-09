# 异常处理与传播约定

> **定位**：项目级异常处理规范 —— 跨模块统一的「何时吞异常、何时抛异常、异常如何传播」约定
> **示例模块**：LLM 服务层（`app/services/llm/` + `app/services/llm_service.py`）—— 项目中最完整的异常处理链路
> **适用**：所有新增/修改代码，尤其是涉及 API 调用、异步生成、可靠性层（重试/熔断/限流）的模块

---

## 📋 目录

- [核心原则](#核心原则)
- [异常分类](#异常分类)
- [分层处理哲学](#分层处理哲学)
- [LLM 模块异常全景](#llm-模块异常全景)
- [传播链路（generate / async_generate / structured）](#传播链路generate--async_generate--structured)
- [关键边界](#关键边界)
- [async generator 的异常语义](#async-generator-的异常语义)
- [CancelledError 的约定](#cancellederror-的约定)
- [配置错误的快速失败](#配置错误的快速失败)
- [异常自然传播 vs 显式 raise](#异常自然传播-vs-显式-raise)
- [LLM 模块自定义异常清单](#llm-模块自定义异常清单)
- [检查清单](#检查清单)
- [相关文档](#相关文档)

---

## 核心原则

1. **可恢复 vs 不可恢复必须区分**：偶发的、可重试的运行时错误（超时/5xx/429）由可靠性层重试消化；不可恢复错误（4xx/认证/熔断/配置错误）必须向上抛，让调用方感知并决策。
2. **有结果才返回，有错误就抛**：facade 层的「返回 None」只用于「业务上无结果」（如解析不出内容），**不用于「发生错误」**——把失败抹平成 None 会让调用方无法区分「超时要不要重试」「key 失效要不要换」「模型就是没说话」。
3. **确定性错误不捕获，快速失败**：配置错误、参数错误、程序员错误（未注册的 key、非法参数）这类「修复配置即消失」的错误，捕获无意义——应该让它抛出暴露，而不是掩盖。
4. **CancelledError 永不吞**：协程取消（`asyncio.CancelledError`）必须向上传播，不得被 `except Exception` 捕获（它是 `BaseException` 子类，`except Exception` 天然不捕获，这点不能靠侥幸——新增代码必须显式确认）。
5. **资源清理用 finally，不用 except 兜底**：预留配额（reservation）等资源的释放放 `finally`，与异常处理分离；`except` 只做「要不要重试/降级」的决策。

---

## 异常分类

### 按「是否可恢复」分三类

| 类别 | 典型 | 处理策略 | 例子 |
| --- | --- | --- | --- |
| **可恢复（重试型）** | 超时、5xx、429 | 可靠性层重试/退避/降级；重试耗尽才向上抛 | `APITimeoutError`、`httpx.TimeoutException`、`RateLimitError` |
| **不可恢复（调用方错误）** | 4xx、认证、熔断开启、配置错误 | **直接向上抛**，调用方决定换模型/修参数/告警 | `BadRequestError`（400）、`AuthenticationError`（401）、`CircuitBreakerOpenError`、`ValueError("未注册")` |
| **业务边界（非传输错误）** | 截断、拒答、工具调用 | 转成**具名异常**短路，调用方差异化处理 | `StructuredTruncationError` / `StructuredRefusalError` / `StructuredToolCallError` |

### 关键：不可恢复错误必须能穿透到调用方

如果 401/配置错误被吞成 None，调用方永远不知道 key 失效或参数错了——只能看到「模型没返回」。工业级网关（LiteLLM 等）把 provider 异常归一化为 `AuthenticationError`/`BadRequestError`/`RateLimitError` 等并**向上抛**，正是为了让调用方能精确处理。

---

## 分层处理哲学

| 层 | 职责 | 异常处理 |
| --- | --- | --- |
| **SDK/网关层**（openai SDK） | 发起请求、网络传输 | **抛异常**（`APIStatusError`/`APITimeoutError`/`APIConnectionError`），永不静默吞掉 |
| **可靠性层**（`retry.py`） | 重试/退避/熔断/fallback | 可恢复错误内部重试消化；重试耗尽 `raise last_exc`（fallback 也失败时主调用异常为主、fallback 异常链 `__cause__`）；不可恢复错误直接 `raise`；熔断开启 `raise CircuitBreakerOpenError` |
| **facade 层**（`llm_service.py`） | 组装请求、编排 | 传输可靠性内部消化；**不可恢复错误向上抛**（或转业务信号）；配置错误在 try 外自然传播 |
| **调用方**（Agent/业务/structured） | 业务决策 | 捕获具名异常按业务处理；未捕获异常记日志/告警 |

**核心约束**：每一层只消化「自己该负责的错误」，不把「上层该知道的错误」吞掉。

---

## LLM 模块异常全景

```
[SDK/HTTP 层]  client.chat.completions.create() 抛
    ① openai.APIStatusError（4xx/5xx/429）—— status_code 区分
    ② openai.APITimeoutError / APIConnectionError（网络）
    ③ httpx 异常（裸透传）
        ↓
[retry.py]  RetryHandler.execute()
    ④ NON_RETRYABLE（4xx/未知）→ raise（直接透传，不重试）
    ⑤ RETRYABLE（超时/5xx）耗尽 → record_failure → fallback 兜底
    ⑥ fallback 失败 → raise 主调用异常（fallback 异常链 __cause__，不覆盖主异常）
    ⑦ 熔断 OPEN（无 fallback）→ raise CircuitBreakerOpenError
        ↓
[llm_service.py]
    ⑧ async_generate()：except Exception → 记日志 + yield build_error_event()（错误进事件流）
    ⑨ generate()：      except Exception → 记日志；NON_RETRYABLE re-raise / 可恢复 return None
        ↓
[structured.py]  StructuredOutput.extract()
    ⑩ StructuredTruncationError → extract 顶层捕获 → return None（截断短路，不降级）
    ⑪ StructuredRefusalError / StructuredToolCallError / 下游不可恢复异常 → 向上抛（调用方差异化处理）
```

**注意 ⑧ 与 ⑨ 的差异**：同样是 `except Exception`，流式转成 SSE 错误事件（错误信息进事件流、调用方可见）；非流式按 B3 契约分流——可恢复错误转 None（调用方降级）、不可恢复错误 re-raise（调用方感知）。两种契约刻意不同，见下节。

---

## 传播链路（generate / async_generate / structured）

### `generate()` —— 非流式，「可恢复失败返回 None，不可恢复抛异常」契约

```
调用方
  └─ generate()
       ├─ _build_chat_kwargs()      ← try 块外：配置错误（get_model ValueError）fail fast 传播
       ├─ retry.execute()           ← try 块内：可恢复错误已内部重试耗尽
       └─ except Exception
            ├─ classify_error == NON_RETRYABLE（4xx/认证/熔断开启/未知）→ raise（向上抛）
            └─ 可恢复（超时/5xx/429）→ return None（调用方按「业务无结果」降级）
```

**设计意图**（B3 契约，2026-08-09）：`generate()` 对**可恢复错误**（超时/5xx/429）可靠性层已重试耗尽后返回 None，调用方（structured）按降级处理；对**不可恢复错误**（4xx/认证/熔断开启/未知异常）向上抛——这些是调用方问题或下游拒绝，降级无意义（会白打降级请求），调用方需感知并决策（修参数/换 key/告警）。**注意**：这个契约是 `generate()` 独有；`async_generate()` 走「错误转事件」契约（见下），两者刻意不同。

### `async_generate()` —— 流式，「错误转事件」契约

```
调用方 async for ... in async_generate()
  ├─ create 失败（阶段1）  → except Exception → yield build_error_event(f"LLM 调用失败: {e!s}") + return
  ├─ 迭代中断（阶段2）     → except Exception → 可整流则重试，不可整流 yield build_error_event(f"流式响应中断: {e!s}") + return
  └─ 硬取消（CancelledError）→ finally 兜底 cancel reservation，异常向上传播
```

**关键**：async_generate 是 **async generator**，异常被捕获后**不是静默吞掉**，而是转成 SSE 错误事件产出。错误通过事件流（`build_error_event`）传达给调用方，错误文案携带异常信息。调用方（Agent 层）收到错误事件即可感知失败。

### `structured.py` —— 业务边界短路

```
extract()
  ├─ 第一级 strict json_schema → _try_extract()
  ├─ 第二级 JSON mode         → _try_extract()
  └─ 第三级 正则提取           → _fallback_extract()
       每个 _try_extract / _fallback_extract：
          ├─ 截断（truncated）    → 本层扩 token 重试 1 次 → 仍失败 raise StructuredTruncationError
          │                        → extract 顶层捕获 → return None（截断短路，不降级）
          ├─ 拒答（refusal）      → raise StructuredRefusalError → 向上抛（调用方安全兜底）
          ├─ 工具调用（tool_calls）→ raise StructuredToolCallError → 向上抛（调用方按工具调用处理）
          ├─ 下游异常（generate 返回 None / 抛异常）→ 降级到下一级
          └─ 正常 → 解析 + Schema 校验
```

**截断/拒答/工具调用均不进降级链**：它们与 response_format 支持度正交，降级无益。截断由 `extract` 顶层捕获返回 None；拒答/工具调用向上抛，调用方需区分「三级耗尽返回 None」与「业务边界异常」。

---

## 关键边界

1. **`except Exception` 只捕获该层该处理的错误**：`generate()`/`async_generate()` 的 `except Exception` 覆盖 `retry.execute` 的调用——配置错误（`_build_chat_kwargs` 内的 `get_model`）在 **try 块外**，能自然穿透不被吞。
2. **请求构建阶段异常永不吞**：`_build_chat_kwargs`、`_count_prompt_tokens` 等「请求组装」代码若产生异常（未注册 key、编码器缺失），应在 try 外 fail fast，而不是被 facade 的 `except Exception` 吞掉。
3. **structured 的 `except Exception` 防什么**（B3 后）：`generate()` 已把可恢复错误转 None、不可恢复错误 re-raise，structured 的 `except Exception` 再做一次分类——`NON_RETRYABLE` re-raise、可恢复降级（兜底防御）。作为兜底合理；真正区分靠 `classify_error`。
4. **可靠性层已重试的异常，上层不要重复处理**：`retry.execute` 内部完成重试/退避/熔断，抛出的就是「最终状态」异常；上层只需决策「要不要降级/短路」，不要再重试。

---

## async generator 的异常语义

对 `async for ... in async_generator()` 的调用方，异常有两种表现：

| 异常来源 | 表现 | 说明 |
| --- | --- | --- |
| generator 内部 `yield build_error_event(...)` | 正常产出错误事件，循环**不抛异常** | 错误以数据（事件字符串）形式传达 |
| generator 内部 `raise`（如 CancelledError 传播） | `async for` 循环抛出异常 | 只有无法转成事件的异常才走这条路 |

**约定**：LLM 流式调用优先用「错误事件」传达失败（符合 SSE 语义），只有取消等无法转为事件的异常才向上抛。新增流式接口应遵循此模式。

---

## CancelledError 的约定

1. **永不吞**：`asyncio.CancelledError` 必须向上传播，不得被 `except Exception` 捕获（它本就是 `BaseException`，`except Exception` 不匹配——但新增代码要**显式确认**，不靠侥幸）。
2. **取消时的资源清理**：reservation 预留等资源在 `finally` 兜底释放（`llm_service` 的 `finally: res.cancel()`），与 `except` 分离——取消发生时 finally 一定会执行。
3. **取消时不再发起新请求**：如半开探针被取消时，`_probe_attempt` 立即传播取消、**不尝试 fallback**（外部取消不应继续发请求）。

---

## 配置错误的快速失败

**反例**：对「未注册的 model_key → `get_model` 抛 `ValueError`」加 try/except——错误是无意义捕获：捕获了也修不好（配置错了，重试/降级都无效），只会掩盖问题。

**正例**：让配置错误 fail fast 传播。它在 `_build_chat_kwargs`（try 块外）抛出，穿透 facade 的异常处理，直接到调用方——开发者立刻看到「Client key 'xxx' 未注册」，修复配置即恢复。

**判断标准**：这个错误「修复配置后是否永久消失」？是 → 不捕获；否（偶发运行时错误）→ 交给可靠性层重试。

---

## 异常自然传播 vs 显式 raise

**两种向上抛异常的方式**，适用场景不同：

```python
# 方式 A：显式 raise —— 捕获后再重抛
try:
    encoder = tiktoken.encoding_for_model(model)
except KeyError:
    raise  # 裸 raise 重抛当前异常

# 方式 B：不捕获，自然传播
encoder = tiktoken.encoding_for_model(model)  # 若抛异常，直接向上传播
```

**原则：能自然传播的异常，就不要包进 try。** 只有当捕获后**还有事要做**（清理资源、记录日志、转换类型）才用显式 raise：

| 场景 | 该怎么做 | 例子 |
| --- | --- | --- |
| 捕获后清理/记录再重抛 | 显式 raise（`except ...: log(...); raise`） | facade 层 `except Exception: log_event(...); return None` |
| 转换异常类型 | `except KeyError: raise ConfigError(...) from e` | 下层 SDK 异常 → 上层业务异常 |
| 主动触发业务边界 | `raise StructuredRefusalError(...)` | structured 拒答短路 |
| **异常自然传播即可满足需求** | **不捕获，让它往上走** | 硬依赖缺失（`ImportError`）、配置错误（`ValueError`）、未知模型编码器异常 |

**反例**：给「能自然传播的异常」加 `except ...: raise` 是死代码——捕获了又原样抛回，多包一层什么都不做的 try/except，只降可读性。

**实例（`_get_encoder`）**：`encoding_for_model(model)` 对未知模型抛 `KeyError`——**捕获并回退** cl100k_base（设计意图，不抛）；`import tiktoken` 失败抛 `ImportError`——**不捕获，自然传播**（tiktoken 是硬依赖，缺失即环境损坏，应 fail fast 暴露，无需显式 raise）。

---

## LLM 模块自定义异常清单

| 异常 | 基类 | 触发 | 调用方处理 |
| --- | --- | --- | --- |
| `CircuitBreakerOpenError` | `Exception` | 熔断 OPEN 且无 fallback | 等待冷却或降级到备用链路 |
| `StructuredExtractionError` | `Exception` | 结构化提取的 API 边界失败基类 | —（中间基类） |
| `StructuredTruncationError` | `StructuredExtractionError` | 截断扩 token 重试后仍不完整 | 由 `extract` 捕获返回 None；业务层可提示「输出过长」 |
| `StructuredRefusalError` | `StructuredExtractionError` | 模型拒答（安全策略） | 捕获转安全兜底/文案，**不强行 repair** |
| `StructuredToolCallError` | `StructuredExtractionError` | 模型选择调用工具而非输出 JSON | 捕获按工具调用走 Agent 循环 |

---

## 检查清单

写代码时对照：

- [ ] **可恢复错误**是否交给了可靠性层重试？（不要在上层重复重试）
- [ ] **不可恢复错误**是否向上抛？有没有被 `except Exception → return None` 意外吞掉？
- [ ] **配置错误**是否在 try 块外 fail fast？（不要在调用点 catch 它）
- [ ] **业务边界**（截断/拒答/工具调用）是否用具名异常短路，而非吞成通用失败？
- [ ] **CancelledError** 是否确认不被 `except Exception` 吞掉？资源释放是否在 `finally`？
- [ ] **async generator** 的错误是否优先转成错误事件，而不是靠抛异常？
- [ ] 吞异常时是否记了日志？（至少 `logger.warning`，丢失错误信息 = 无法排查）

---

## 相关文档

- [LLM 服务层说明](service_doc/llm_doc/llm.md)（generate / async_generate 的调用契约）
- [重试与熔断](service_doc/llm_doc/retry.md)（`classify_error` 错误分类白名单、`CircuitBreakerOpenError`）
- [结构化输出](service_doc/llm_doc/structure.md)（四态分类、`StructuredTruncationError`/`RefusalError`/`ToolCallError`）
- [限流器](service_doc/llm_doc/limiter.md)（reserve/settle 资源清理、取消兜底）
- [全局日志框架](logging.md)（`log_event_async("llm_call")` 错误记录）
