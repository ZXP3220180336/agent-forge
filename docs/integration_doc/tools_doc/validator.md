# 参数校验器（ParameterValidator）说明文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/validator.py`
> **职责**：工具参数 JSON Schema 严格校验 + 错误归因（中文可读，供 LLM 下一轮修正）
> **状态**：✅ 已实现
> **工业级对照**：jsonschema 完整校验（类型 / 必填 / 枚举 / 范围），对齐 OpenAI Function Calling 参数约束

---

## 📋 目录

- [参数校验器（ParameterValidator）说明文档](#参数校验器parametervalidator说明文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [iter\_errors 全量收集](#iter_errors-全量收集)
    - [reject\_unknown 包装](#reject_unknown-包装)
    - [不做类型转换](#不做类型转换)
  - [对外接口](#对外接口)
  - [错误归因模板](#错误归因模板)
  - [边界情况](#边界情况)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [相关文档](#相关文档)

---

## 设计目标

1. **完整校验**：不只查「未知参数 + 必填」，用 jsonschema 覆盖类型 / 枚举 / 数值范围 / 字符串长度 / 模式（Draft 2020-12）
2. **一次反馈全部问题**：`iter_errors` 全量收集，让 LLM 下一轮全部修正，减少归因往返
3. **错误可归因**：校验失败输出中文结构化描述（含字段名、期望值、实际值），LLM 可据此自我纠正
4. **严格 fail-fast**：不做类型转换，杜绝用强制转换掩盖 LLM 输出质量问题

## 核心概念解释

### iter_errors 全量收集

`jsonschema.validate()` 只报第一个错误；`Draft202012Validator(schema).iter_errors(instance)` 迭代全部错误。参数多错时一次返回所有问题，LLM 单轮即可修正完毕。

### reject_unknown 包装

默认 `reject_unknown=True`，校验前给 schema 包一层 `additionalProperties: False`——LLM 幻觉出的无关参数立即被拒绝，而不是静默忽略。需要宽松场景可 `ParameterValidator(reject_unknown=False)` 放行。

### 不做类型转换

LLM 传 `"count": "3"`（字符串）给 integer 参数 → 直接校验失败并归因，而非自动转 int。强制转换掩盖输出质量问题，破坏「失败 → 明确错误 → LLM 重试」的归因闭环。

## 对外接口

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `validate` | `(schema: dict, parameters: dict) -> list[ValidationIssue]` | 全量校验；空列表 = 通过 |
| `format_issues` | `(issues: list[ValidationIssue]) -> str` | 分号拼接全部问题为一句（供调用方拼接错误信息） |
| `validate_or_raise` | `(schema: dict, parameters: dict) -> None` | 有错抛 `ParameterValidationError`（需要异常语义的调用方） |

- `ValidationIssue`：`dataclass(frozen=True)`，字段 `message: str`（中文完整描述，含字段名）
- `ParameterValidationError(ValueError)`：携带归因错误信息

**调用方**：executor 校验失败时经 `tool.validation_issues()` 取问题列表构造错误；`BaseTool.validate_parameters()` 委托本校验器（返回布尔）。

## 错误归因模板

| jsonschema validator | 输出示例 |
| --- | --- |
| `required` | 缺少必填参数 'file_path' |
| `type` | 参数 'count' 类型应为 integer，实际为 string |
| `enum` | 参数 'mode' 必须是 ['fast', 'slow'] 之一，实际为 'turbo' |
| `additionalProperties` | 参数 'extra' 不在 schema 允许范围内 |
| `minimum` / `maximum` / `minLength` 等 | 参数 'age' -1 is less than the minimum of 0（兜底 `error.message`） |

## 边界情况

1. **parameters 为空 dict**：仅 required 校验生效，其余参数无约束时通过
2. **schema 无 properties**：仅 reject_unknown 生效（任何参数都被拒绝）
3. **根级错误**（如参数非 object）：`path` 为空，兜底字段名取「参数」（无具体字段名，归因信息为「参数 '参数' …」）
4. **类型映射**：Python 类型名（`str`/`int`/`list`）映射为 JSON Schema 类型名（`string`/`integer`/`array`），错误信息语义一致
5. **reject_unknown 幂等**：校验前无条件包一层 `additionalProperties: false`（重复置 false 幂等，无需特殊处理）

## 测试状态

`tests/unit/test_tool_validator.py`（13 用例）：类型 / 必填 / 枚举 / 范围 / 未知参数拒绝与放行 / 全量收集 / format_issues / validate_or_raise / BaseTool 委托。

## 设计决策

- 严格校验 + 拒绝未知 + 不类型转换 + 错误归因 → [ADR](../../../adr/integration/tools/2026-08-17-jsonschema-strict-validation.md)

## 相关文档

- [工具模块接口文档](tools.md)（BaseTool.validate_parameters 契约）
- [ToolService 执行流程](../../../app/integration/tools/executor.py)（校验接入点）
