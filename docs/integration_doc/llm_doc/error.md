# llm/errors.py 传输错误处理设计文档

> **模块**：`app/integration/llm/errors.py`
> **更新日期**：2026-09-01
> **职责**：LLM 传输异常的统一理解与决策——分类（`classify_error`）/ 归一（`normalize_transport_error`）/ 降级判定（`is_unsupported_response_format_error`）/ 下游决策（`decide_downstream_error`），llm 模块错误处理单一归属
> **状态**：✅ 已实现
> **配套**：分类契约（`ErrorCategory` / `ErrorClassifier`）与实现同属本模块；归一目标 `LLMAPIError` 在 `app/shared/exceptions.py`（AppError 树）；与 `AgentErrorKind`（Agent 编排分发，error_handling.py）、工具层 `ErrorCode` 正交

---

## 📋 目录

- [llm/errors.py 传输错误处理设计文档](#llmerrorspy-传输错误处理设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [传输错误分类（ErrorCategory）](#传输错误分类errorcategory)
    - [归一（normalize\_transport\_error）](#归一normalize_transport_error)
    - [降级判定（is\_unsupported\_response\_format\_error）](#降级判定is_unsupported_response_format_error)
    - [下游决策（decide\_downstream\_error）](#下游决策decide_downstream_error)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
  - [对外接口](#对外接口)
  - [边界情况](#边界情况)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [问题记录](#问题记录)
  - [相关文档](#相关文档)

---

## 设计目标

1. **错误知识单一归属**：传输异常白名单 / 分类 / 归一 / 降级判定从重试机制（retry.py）与降级链（structured.py）收敛到本模块——`retry.py` 只做重试/熔断，`structured.py` 只做结构化降级，错误理解与决策不再散落
2. **统一下游决策**：`generate` 下游异常（可恢复耗尽 → 降级 / openai 不可恢复 → 归一上抛 / 非 openai → 原样上抛）由 `decide_downstream_error` 统一产出，llm_service / structured 只消费结果，消除重复决策分支
3. **契约与实现同层**：`ErrorCategory` 是 LLM 传输层分类语言，仅集成层 LLM 消费（领域/应用层不引用），故契约随实现归本模块，不入 shared（shared 是被所有层引用的零依赖核心库，单一消费方的契约不属其列）
4. **领域层可兜底**：openai 不可恢复异常归一为 `LLMAPIError`（AppError 树），领域层 `except AppError` 统一接住——Reflection 自查/修正降级闭环（REASON-010）

## 核心概念解释

### 传输错误分类（ErrorCategory）

LLM 传输错误的**可恢复性**分类，决定处理策略（重试 / 退避 / 熔断）：

| 类别 | 触发 | 处理 |
| --- | --- | --- |
| `RETRYABLE` | 网络层故障（openai 封装 / 裸 httpx）、超时、5xx | 重试 + 计入熔断窗口 |
| `RATE_LIMITED` | 429 | 退避重试（尊重 Retry-After），**不计入熔断** |
| `NON_RETRYABLE` | 4xx、响应校验错误、token 截断、内容被过滤、**未知异常（默认兜底）** | 直接抛出不重试 |

> 分类规则为**白名单映射，未知异常默认不可重试**——避免对重试无效的错误盲目重试打下游。完整分类矩阵见下（组件详解）。

### 归一（normalize_transport_error）

把 openai 不可恢复传输异常包装为 `LLMAPIError`（AppError 树，`NonRetryableError` 子类），让领域层 `except AppError` 统一兜底：

- **归一**：`openai.APIStatusError`（4xx/认证，携带 `status_code`）+ `_NON_RETRYABLE_EXC`（响应校验 / 长度截断 / 内容过滤，`status_code=None`）
- **不归一**（返回 None）：429 / 5xx / 超时（可恢复，重试耗尽走降级）、非 openai 异常（熔断 `CircuitBreakerOpenError` / 编程错误，保持原语义）
- **`status_code` 保留是硬约束**：`is_unsupported_response_format_error` 依赖 `status_code==400` + message 关键词判定「response_format 不被支持」降级，不保留该降级链即断裂

### 降级判定（is_unsupported_response_format_error）

判断「模型/网关不支持 response_format」的 400 错误（错误信息含 `response_format` 或 `json_schema` 字样）。这类错误不是「模型能力不足需修复」，而是「该约束模式不支持」——应降级到下一级（JSON mode / 正则），而非当致命错误上抛。对已归一的 `LLMAPIError(status_code=400)` 同样生效（降级链存活）。

### 下游决策（decide_downstream_error）

`generate` 下游异常的统一决策，产出 `DownstreamDecision`：

| 异常分类 | 决策结果 |
| --- | --- |
| RETRYABLE / RATE_LIMITED（可靠性层已重试耗尽） | `to_raise=None` → 调用方降级（return None，业务无结果） |
| NON_RETRYABLE + openai 异常 | `to_raise=LLMAPIError`（`normalized=True`，调用方 `raise ... from 原异常`） |
| NON_RETRYABLE + 非 openai（熔断 / 编程错误） | `to_raise=原样`（`normalized=False`，裸 raise 保留 traceback） |

> `unsupported_response_format`（400 降级下一级）**不在本函数**：仅 structured 降级链需要（llm_service Facade 边界不降级下一级，仍归一上抛 `LLMAPIError(400)`），由 structured 调用 `is_unsupported_response_format_error` 特判并保留降级诊断日志。

## 架构总览

```text
llm/errors.py（传输错误理解与决策）
    ├── classify_error ──────────────→ retry.py（重试/熔断分类）、streaming_rectifier.py（整流重试判定）
    ├── decide_downstream_error ─────→ llm_service.py（Facade 边界：归一上抛 / 降级）
    ├── decide_downstream_error ─────→ structured.py（降级链：降级下一级 / 上抛）
    └── is_unsupported_response_format_error → structured.py（400 降级下一级特判）
```

| 消费方 | 用途 | 位置 |
| --- | --- | --- |
| `retry.py` | 按分类决定重试策略（NON_RETRYABLE 直接抛 / 其余退避） | RetryHandler.execute |
| `llm_service.py` | generate except 统一决策（归一上抛 / 降级） | generate |
| `structured.py` | 降级链决策 + 400 unsupported 特判 | `_call_generate` |
| `streaming_rectifier.py` | 流式整流重试判定（RETRYABLE / RATE_LIMITED 才整流） | `_should_rectify` |

**依赖方向**：本模块只依赖 `shared`（`LLMAPIError`）+ openai SDK，被 llm 包内各组件引用，不反向依赖。

## 组件详解

```python
class ErrorCategory(Enum):
    RETRYABLE = "retryable"      # 可重试（超时、5xx）
    NON_RETRYABLE = "fatal"      # 不可恢复（认证、参数错误、未知兜底）
    RATE_LIMITED = "rate_limited"  # 限流（退避重试，不计入熔断）

ErrorClassifier = Callable[[Exception], ErrorCategory]  # classify_error 函数签名契约

@dataclass(frozen=True)
class DownstreamDecision:
    to_raise: Exception | None = None   # 需上抛；None = 降级（return None）
    normalized: bool = False            # to_raise 是否 LLMAPIError（调用方需 raise ... from 原异常）
```

**分类矩阵**（`classify_error` 白名单映射）：

| 异常 / HTTP 状态 | 分类 |
| --- | --- |
| `TimeoutError` / `APITimeoutError` / `APIConnectionError` / 裸 httpx 网络异常 / 5xx | RETRYABLE |
| 429 / `RateLimitError` | RATE_LIMITED |
| 4xx（400/401/403/404/405/409/413/422 等） | NON_RETRYABLE |
| `APIResponseValidationError`（响应 schema 不匹配） | NON_RETRYABLE |
| `LengthFinishReasonError`（token 截断）/ `ContentFilterFinishReasonError`（内容被过滤） | NON_RETRYABLE |
| 未知异常（无 status_code、非已知类型） | NON_RETRYABLE（默认兜底） |

> 坑：`InternalServerError` 无硬编码 status_code（继承 `APIStatusError` 但状态码是响应里的实际值）——分类须走 `status_code` 分支（5xx → RETRYABLE），不能依赖 `isinstance`。

## 对外接口

> 对外接口 = 被 llm 包内组件 / 上层捕获的公共入口。`_` 前缀白名单与内部辅助不进此栏。

| 方法 | 同步 | 说明 |
| --- | --- | --- |
| `classify_error(exc: Exception) -> ErrorCategory` | 是 | 传输异常分类（RETRYABLE / RATE_LIMITED / NON_RETRYABLE） |
| `normalize_transport_error(exc: Exception) -> LLMAPIError \| None` | 是 | openai 不可恢复异常 → `LLMAPIError`（status_code 保留） |
| `is_unsupported_response_format_error(exc: Exception) -> bool` | 是 | 400 + response_format/json_schema 关键词判定 |
| `decide_downstream_error(exc: Exception) -> DownstreamDecision` | 是 | 下游异常统一决策（归一上抛 / 原样上抛 / 降级） |
| `ErrorCategory`（枚举） | — | 分类值（`RETRYABLE` / `RATE_LIMITED` / `NON_RETRYABLE`） |
| `DownstreamDecision`（数据类） | — | 决策结果（`to_raise` / `normalized`） |

## 边界情况

1. **status_code 保留是硬约束**：`normalize_transport_error` 必须透传 `status_code` + message——`is_unsupported_response_format_error` 依赖它判定 400 降级，不保留则降级链断裂（test_generate_structured 既有用例保护）
2. **unsupported 400 降级下一级保留在 structured**：llm_service Facade 边界不降级下一级（归一上抛 `LLMAPIError(400)`），structured 特判降级并保留诊断日志——两处语义差异不并入 `decide_downstream_error`
3. **编程错误不吞（fail-fast）**：`decide_downstream_error` 对非 openai 编程错误原样上抛（`to_raise=原样`）——吞掉会掩盖真实 bug（REASON-010 边界）
4. **可恢复错误重试耗尽 → 降级**：`RETRYABLE` / `RATE_LIMITED` 由可靠性层重试耗尽后 `to_raise=None`，调用方 return None（业务无结果）
5. **未知异常默认 NON_RETRYABLE**：无法分类的异常不盲目重试，直接上抛

## 测试状态

- `tests/unit/test_errors.py`（16 用例）：`normalize_transport_error`（APIStatusError 保留 status_code / 非 HTTP 无 status_code / 可恢复与非 openai 不归一）、`is_unsupported_response_format_error`（400 + 关键词 / 非 400 / LLMAPIError 判定存活）、`decide_downstream_error` 决策矩阵（可恢复→降级 / openai 401→归一上抛 / unsupported 400 不降级 / 熔断与编程错误→原样上抛）、`DownstreamDecision` 契约
- `tests/unit/test_classify_error.py`（18 用例）：分类矩阵全量（RETRYABLE / RATE_LIMITED / NON_RETRYABLE）
- `tests/unit/test_error_category.py`（3 用例）：契约归属（枚举值 / ErrorClassifier / 返回同一枚举实例）
- 跨模块间接覆盖：`test_llm_service.py`（归一决策）、`test_generate_structured.py`（400 降级链）、`test_reflection.py`（LLMAPIError → 领域层降级）、`test_stream_rectify.py` / `test_retry.py`（分类消费）

## 设计决策

> 设计决策已归档至 ADR，完整决策（Context → Decision → Consequences）见：

- [openai 异常归一（LLMAPIError）](../../../adr/integration/llm/2026-09-01-openai-error-normalization.md)：集成层 `generate` 边界把 openai 不可恢复异常归一进 AppError 树，领域层 `except AppError` 统一兜底；`raise ... from e` 保留原始异常；`async_generate` 流式不归一
- **契约归属（ErrorCategory 不入 shared）**：ErrorCategory 是 LLM 传输层分类，单一消费方（集成层 LLM），契约随实现归本模块——上移 shared 的决策已修正（见 [todo.md](../../../docs/todo.md) 顶部「契约归属修正」）

## 问题记录

> 问题生命周期（发现 → 分析 → 修复 → 验证 → 教训）见：

- [REASON-010 不可恢复错误未降级](../../../issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md)：Reflection 自查/修正阶段 openai 4xx/认证冒泡崩溃 → 归一 `LLMAPIError` 闭环

## 相关文档

- [LLM 网关对外接口文档](llm.md)（Facade 契约 + 对外异常）
- [RetryHandler 设计文档](retry.md)（重试/熔断机制，消费 classify_error）
- [StructuredOutput 设计文档](structure.md)（结构化降级，消费 decide + unsupported 判定）
- [StreamingRectifier 设计文档](streaming_rectifier.md)（流式整流，复用 classify_error）
- [错误处理与传播约定](../../shared_doc/error_handling.md)（异常树 / 四类码边界）
- [集成层说明](../README.md)
