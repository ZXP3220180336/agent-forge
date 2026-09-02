# Reflection 自查/修正阶段不可恢复错误未降级：AppError 家族冒泡（REASON-010）

> **模块**：`app/domain/reasoning/reflection.py`（ReflectionStrategy 自查/修正阶段）
> **发现日期**：2026-09-01 ｜ **状态**：✅ 已修复（熔断等 AppError）｜ **遗留**：✅ 已闭环（集成层异常归一，见下方「遗留闭环」） ｜ **编号**：REASON-010

## 发现

工业级完成度评审发现：`_critique` / `_refine` 的异常捕获只覆盖 `(StructuredRefusalError, StructuredToolCallError)`，漏掉会从 `LLMService.generate_structured` 实际冒泡的**不可恢复错误**。逐层核实后的失败信号出口：

| 集成层失败信号 | 是否冒泡到 Reflection | 类型 |
| --- | --- | --- |
| 拒答 `StructuredRefusalError` / 工具调用 `StructuredToolCallError` | ✅ 冒泡（extract 不捕获） | AppError 树 |
| 截断 `StructuredTruncationError` | ❌ **不冒泡**——`StructuredOutput.extract` 三处短路 `return None`（[structured.py](../../../app/integration/llm/structured.py) extract 入口） | — |
| 熔断 `CircuitBreakerOpenError` | ✅ 冒泡（retry 抛 → generate 对 NON_RETRYABLE `raise`） | AppError 树 |
| openai 4xx / 认证（`APIStatusError` 系列） | ✅ 冒泡（同上） | ❌ **非 AppError**（openai SDK 异常） |

**后果（熔断场景）**：自查 / 修正阶段遇熔断开启时，`CircuitBreakerOpenError` 冒泡出 `execute()`，违反模块「失败降级（best-effort），不抛错」承诺。对比 ReAct 收集阶段同样的错误会被 `outcome.error` 捕获降级——**护栏能力不对称**。

## 分析

1. **根因**：异常捕获范围锚定「结构化两类特殊错误」而非「统一异常树根」。集成层 `generate_structured` 的失败信号出口有：拒答/工具调用/截断（`StructuredExtractionError` 三子类）+ 不可恢复错误（熔断 / openai 4xx）+ 可恢复错误（内部重试耗尽 → `return None`）。Reflection 只处理了其中两类。
2. **捕获范围决策**：捕获 **`AppError`（异常树根）**——覆盖 `StructuredExtractionError` 三子类 + 熔断 `CircuitBreakerOpenError`，统一走 CRITIQUE_FAILED 分发（默认 CONTINUE → 降级采用最近稿）。**编程错误（TypeError 等非 AppError）必须继续冒泡**——吞掉会掩盖真实 bug（fail fast）。
3. **边界（诚实记录）**：openai `APIStatusError`（4xx / 认证）**不属于 AppError 树**，`except AppError` 捕获不到——该异常未归一是集成层遗留缺口（`llm_service.generate` 对 NON_RETRYABLE 直接 `raise` 原始异常，未包装进统一异常树）。领域层不 import openai 类型（违反依赖方向），故本层修复不覆盖；**已于 2026-09-01 由集成层异常归一闭环**（见下方「遗留闭环」）。
4. **安全性确认**：
   - `_dispatch_critique_failed_result` 内的 `raise AgentRunError`（RAISE 决策）位于 except 块内，Python 不会让同一 except 再次捕获自己主动抛出的异常，RAISE 语义不受影响。
   - 拒答 message 已在集成层截断（structured.py 只保留 finish_reason，不含拒答原文），Reflection 拼接 `str(e)` 不泄漏敏感内容。
   - 默认 `CRITIQUE_FAILED` action = CONTINUE（error_handling.py 默认表），零行为变化兜底。

## 修复

`app/domain/reasoning/reflection.py`：`_critique` / `_refine` 的 except 从 `(StructuredRefusalError, StructuredToolCallError)` 扩展为 `AppError`，docstring 同步说明捕获范围与 fail-fast 边界。

> 迭代修正记录：初版将「截断」列入缺口并锚定 2 个测试，经审查核实 `StructuredOutput.extract` 对截断短路返回 `None`（不向上抛）后，删除虚假锚定、更正文档——截断走既有「自查/修正返回 None → 降级」路径，本层无缺口。

## 验证

`tests/unit/test_reflection.py` 新增 3 用例（修复前不可恢复降级用例失败、编程错误冒泡用例通过，复现问题）+ 1 防回归：

- `test_reflect_critique_nonretryable_degrades_to_draft`：自查抛 NonRetryableError（熔断基类）→ 降级采用初稿
- `test_reflect_refine_nonretryable_degrades_to_draft`：修正抛 NonRetryableError → 降级采用初稿（refine_rounds=0）
- `test_reflect_critique_bug_propagates`：自查抛 TypeError（非 AppError）→ 向上冒泡（防过度捕获回归）

验证结果：test_reflection.py（22 用例）+ test_reflection_agent.py（5 用例）通过，全量 pytest 无回归。

## 教训

1. **错误捕获范围锚定异常树根，而非枚举已知错误**：集成层异常面会演进（新增截断类别、不可恢复错误上抛），按具体类型枚举会随演进漏项。业务/系统级错误应统一捕获树根，编程错误留待冒泡。
2. **降级承诺必须按「调用链上每条失败出口」逐条核验**：一个阶段承诺 best-effort，就要穷举其依赖组件（LLM 网关）所有失败信号出口（返回 None / 抛 Structured 异常 / 抛不可恢复异常），逐条确认落入降级路由，而非只覆盖「当前已知」的少数几种。本次「截断」即因未追踪 `extract` 入口的短路捕获而误判。
3. **集成层异常归一需闭环**：`llm_service.generate` 对 NON_RETRYABLE 直接 `raise` openai 原始异常，导致领域层 `except AppError` 无法统一兜底。统一异常树的价值依赖「所有透出边界的异常都归一为 AppError」——**已于 2026-09-01 闭环**：`generate` 边界把 openai `APIStatusError` 系列归一为 `LLMAPIError`（AppError 树，携带 status_code，`raise ... from e` 保留原始异常），见 [ADR openai-error-normalization](../../../adr/integration/llm/2026-09-01-openai-error-normalization.md)。

## 遗留闭环（2026-09-01）

**集成层异常归一落地**（openai 4xx / 认证 → AppError 树）：

- `app/shared/exceptions.py` 新增 `LLMAPIError(NonRetryableError)`（`code=LLM_API_ERROR`，携带 `status_code`）。
- `app/integration/llm/errors.py`（传输错误处理单一归属）承载 `normalize_transport_error(exc)`：包装 `APIStatusError`（带 status_code）+ 非 HTTP 永久性异常（status_code=None）；其余返回 None（429/5xx/超时/非 openai 原样）。
- `app/integration/llm/llm_service.py` `generate` except 边界经 `decide_downstream_error` 决策：NON_RETRYABLE openai 异常 → `raise LLMAPIError(...) from e`（归一）；非 openai 原样上抛；可恢复（RETRYABLE / RATE_LIMITED）重试耗尽 return None。
- `ErrorCategory` 分类契约与实现同属 `app/integration/llm/errors.py`（单一消费方——仅集成层 LLM，契约不入 shared；上移 shared 的决策已修正，相关 ADR 已删除）。
- **验证**：`tests/unit/test_reflection.py` 新增 `test_reflect_critique_llm_api_error_degrades_to_draft` / `test_reflect_refine_llm_api_error_degrades_to_draft`（fake 抛 `LLMAPIError(401/403)` → 降级采用最近稿）；`tests/unit/test_llm_service.py` 新增 4 例归一测试（401 → LLMAPIError + `__cause__`、APIResponseValidationError → status_code=None、ValueError 透传、APITimeoutError → return None）；`tests/unit/test_error_category.py` 锚定契约归属。全量 pytest 通过。
