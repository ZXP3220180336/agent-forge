# 工具参数校验：jsonschema 严格校验 + 错误归因

> **状态**：✅ 已采纳
> **决策日期**：2026-08-17
> **涉及模块**：`app/integration/tools/validator.py`（ParameterValidator）· `app/integration/tools/base.py`（validate_parameters 委托）
> **关联文档**：[validator.md](../../../docs/integration_doc/tools_doc/validator.md)

---

## Context

- 原参数校验仅用自定义逻辑查「未知参数 + 必填缺失」，不校验类型 / 枚举 / 范围——LLM 传 `"count": "3"`（字符串）给 integer 参数会被静默接受，执行时报错或产生错误结果
- 工业界标准（DeepSeek-Harness / 阿里等）是 JSON Schema 驱动校验：映射 → 类型转换 → 校验，构建「闸门」隔离不可信输入
- `jsonschema` 已在项目依赖（pyproject.toml），无需新增

## Decision

**引入 jsonschema（Draft 2020-12）做完整参数校验，`BaseTool.validate_parameters()` 委托校验器，保持布尔签名兼容。**

- **iter_errors 全量收集**：一次返回全部错误（非 validate 只报首个），LLM 下一轮全部修正，减少归因往返
- **拒绝未知参数**：`reject_unknown=True`（默认）给 schema 包 `additionalProperties: False`，LLM 幻觉出的无关参数立即被拒绝；需宽松场景可 `reject_unknown=False`
- **不做类型转换**：严格 fail-fast，让 LLM 收到「类型应为 X」后自我纠正——强制转换掩盖 LLM 输出质量问题，破坏「失败 → 明确错误 → LLM 重试」归因闭环
- **错误中文归因**：按 jsonschema validator 类型映射中文模板（required→"缺少必填参数 'x'"、type→"类型应为 integer，实际为 string"、enum→"必须是 [...] 之一"），错误信息可直接喂回 LLM
- **`validate_parameters` 签名兼容**：返回布尔（`not validation_issues()`），内置工具 execute 内 `if not self.validate_parameters(...)` 零改动

## Consequences

- **正面**：校验能力与 OpenAI Function Calling 参数约束对齐（类型 / 枚举 / 范围 / 未知全查）；错误可归因，LLM 纠错效率提升；校验器独立组件可测试、可替换
- **负面**：**行为变更**——LLM 此前传的字符串化数字（如 `"count": "3"`）会从「执行」变「校验失败并归因」，这是严格化的预期效果；类型校验不提供开关（语义不可降级），仅未知参数可放宽
