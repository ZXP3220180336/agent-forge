# 错误分类：未知/未覆盖异常默认 RETRYABLE，盲目重试

> **状态**：✅ 已修复（2026-08-01）
> **优先级**：P1（中）
> **来源**：2026-08-01 工业级改造 · 2026-08-16 从 retry.md 提取归档
> **涉及模块**：`app/integration/llm/retry.py`（`classify_error`）
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md)

---

## 问题描述

### 现象

只显式分类 400/401/403/422/429/5xx/超时，其余（404/405/413 等 4xx、非 HTTP 异常、裸 httpx 网络异常）落入 RETRYABLE 兜底 → 重试无效的错误白打下游 N 次并计入熔断窗口。

### 影响

对重试无效的错误盲目重试（浪费配额、放大熔断窗口）；未知异常默认重试不可控。

### 根因

分类是「黑名单式」（显式列出不可重试），未覆盖的异常默认可重试。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 白名单映射 | 显式列出可重试异常（网络/超时/5xx），未知异常默认不可重试——避免对无法恢复的错误盲目重试 |

---

## 修复方案（含决策取舍）

**决策**：白名单映射——4xx 全部 NON_RETRYABLE；显式捕获 openai 网络异常 + 裸 httpx 异常 → RETRYABLE；响应校验/截断/内容过滤 → NON_RETRYABLE；**未知异常默认 NON_RETRYABLE**。

**修复要点**：

- `_RETRYABLE_EXC`（网络层）：`TimeoutError` / `APITimeoutError` / `APIConnectionError` + 裸 `httpx.TimeoutException` / `httpx.NetworkError`；
- 5xx → RETRYABLE（走 `status_code` 分支，不能依赖 `isinstance(InternalServerError)`——它无硬编码状态码）；
- 429 → RATE_LIMITED；4xx → NON_RETRYABLE；
- `APIResponseValidationError` / `LengthFinishReasonError` / `ContentFilterFinishReasonError` → NON_RETRYABLE；
- **未知异常默认 NON_RETRYABLE**（兜底）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/retry.py` | `classify_error` 白名单映射 + 未知默认 NON_RETRYABLE | `test_classify_error.py` 全异常分类用例 |

---

## 验证

- 各类异常正确分类；未知异常不重试
- 全量测试通过（2026-08-01 改造时验证）

---

## 教训沉淀

- **错误分类用白名单而非黑名单**：显式列出可重试的（网络/超时/5xx），未知异常默认不可重试——避免对无法恢复的错误盲目重试打下游。
- **`InternalServerError` 无硬编码状态码**：5xx 判定必须走 `status_code` 分支，不能依赖 `isinstance`。
