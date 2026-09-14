# 参数校验器（ParameterValidator）说明文档

> **更新日期**：2026-09-14
> **模块**：`app/integration/tools/validator.py`
> **职责**：工具参数 JSON Schema 严格校验 + 错误归因（中文可读，供 LLM 下一轮修正）
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
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
  - [验证入口](#验证入口)
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

`jsonschema.validate()` 选择一个代表错误；本组件通过共享 `create_schema_validator` 固定使用 Draft 2020-12，并用 `iter_errors(instance)` 收集全部实例错误。参数多错时一次反馈全部问题，供模型下一轮修正。版本声明、子 Schema、引用范围及 `format` 行为统一见[本地 JSON Schema 契约](../../shared_doc/json_schema.md)。

### reject_unknown 包装

默认 `reject_unknown=True`，在根 Schema 副本上设置 `additionalProperties: False`，拒绝未知参数；不递归收紧独立的子 Schema。`reject_unknown=False` 时保留调用方原约束，并不覆盖其显式 `additionalProperties: false`。

先预检原 Schema，避免包装覆盖非法 `additionalProperties`，或新增该字段意外修复原本悬空的引用。`reject_unknown=False` 或原定义已为 `additionalProperties: false` 时复用原校验器；其他情况为有效副本重新构建校验器。原定义和有效副本的预检失败均转为定义错误问题列表。不能用原校验器的 `evolve(schema=effective)` 代替重建：它可能沿用原解析器，令 `$ref: "#"` 返回未收紧的原 Schema。重新构建保证递归根引用同样遵守未知参数限制，整个过程不修改调用方对象。

### 不做类型转换

LLM 传 `"count": "3"`（字符串）给 integer 参数 → 直接校验失败并归因，而非自动转 int。强制转换掩盖输出质量问题，破坏「失败 → 明确错误 → LLM 重试」的归因闭环。

## 对外接口

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `validate` | `(schema: dict, parameters: dict) -> list[ValidationIssue]` | 全量校验；空列表 = 通过；原 Schema 预检失败转为定义错误问题 |
| `format_issues` | `(issues: list[ValidationIssue]) -> str` | 分号拼接全部问题为一句（供调用方拼接错误信息） |
| `validate_or_raise` | `(schema: dict, parameters: dict) -> None` | 有错抛 `ParameterValidationError`（需要异常语义的调用方） |

- `ValidationIssue`：`dataclass(frozen=True)`，字段 `message: str`（中文完整描述，含字段名）
- `ParameterValidationError(ValueError)`：携带归因错误信息

**调用方**：executor 校验失败时经 `tool.validation_issues()` 取问题列表构造错误；`BaseTool.validate_parameters()` 委托本校验器（返回布尔）。

原 Schema 预检抛出的 `SchemaError` 转为 `ValidationIssue("Schema 定义无效: ...")`。因此布尔接口返回 `False`，`validate_or_raise` 继续抛 `ParameterValidationError`；经 ToolService 执行时返回 `ErrorCode.VALIDATION`，不启动工具执行。定义错误需要修正工具 Schema，不能通过改变模型参数修复。

模型协议导出由 BaseTool 在发送前检查同一份 `parameters` 快照，失败直接抛 `SchemaError`；该导出异常与执行侧的问题列表属于不同入口契约，见 [BaseTool 抽象契约](tools.md#basetool-抽象契约)。

## 错误归因模板

| jsonschema validator | 输出示例 |
| --- | --- |
| `required` | 缺少必填参数 'file_path' |
| `type` | 参数 'count' 类型应为 integer，实际为 string |
| `enum` | 参数 'mode' 必须是 ['fast', 'slow'] 之一，实际为 'turbo' |
| `additionalProperties` | 参数 'extra' 不在 schema 允许范围内 |
| `minimum` / `maximum` / `minLength` 等 | 参数 'age' -1 is less than the minimum of 0（兜底 `error.message`） |

## 边界情况

1. **parameters 为空 dict**：仍检查 `required`、`minProperties` 等适用约束，全部通过才返回空问题列表
2. **schema 无 properties**：默认未知参数策略仍生效；`patternProperties` 等其他关键字按 Schema 语义共同参与判定
3. **根级错误**（如参数非 object）：`path` 为空，兜底字段名取「参数」（无具体字段名，归因信息为「参数 '参数' …」）
4. **类型映射**：Python 类型名（`str`/`int`/`list`）映射为 JSON Schema 类型名（`string`/`integer`/`array`），错误信息语义一致
5. **reject_unknown 幂等**：原 Schema 合法时，在副本重复设置 `additionalProperties: false` 不改变结果；根递归引用也必须以此副本为解析起点
6. **不支持的声明或引用**：预检阶段返回定义错误；不会等待对应字段出现在参数中才发现嵌套 Schema 问题

## 验证入口

[参数校验测试](../../../tests/unit/test_tool_validator.py)覆盖类型、必填、枚举、范围、未知参数策略、全部错误收集、布尔及异常接口；同时验证版本声明、非法定义、依赖约束、同一导出快照、递归根引用和经 ToolService 拦截后的零工具执行。[执行组件测试](../../../tests/unit/test_tool_executor_components.py)覆盖既有校验归因与审计路径。实际运行状态统一见 [ALIGNMENT](../../ALIGNMENT.md)。

## 设计决策

- 严格校验 + 拒绝未知 + 不类型转换 + 错误归因 → [ADR](../../../adr/integration/tools/2026-08-17-jsonschema-strict-validation.md)
- 固定方言、Schema 接入边界与共享校验工厂 → [本地 JSON Schema 方言统一决策](../../../adr/2026-09-14-json-schema-dialect.md)

## 相关文档

- [工具模块接口文档](tools.md)（BaseTool.validate_parameters 契约）
- [ToolService 执行流程](../../../app/integration/tools/executor.py)（校验接入点）
