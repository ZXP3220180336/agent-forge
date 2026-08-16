# LLM-009 strict + additionalProperties: true 第一级必然 400 且归因误导

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 8）
> **涉及模块**：`app/integration/llm/structured.py`（`_build_json_schema_request` / `_enforce_no_extra_fields`）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md)

---

## 问题描述

### 现象

`_build_json_schema_request` 固定 `"strict": True`，schema 直接透传；而 `_enforce_no_extra_fields` 显式尊重 `additionalProperties: true`（调用方已写 true 保持 true）。OpenAI strict JSON Schema 禁止 `additionalProperties: true`——此类 schema 第一级必然 400，且被 `_is_unsupported_response_format_error`（400 + message 含 json_schema 关键词）误归因为「模型不支持 response_format」→ 白打一次调用 + 排障归因误导。

### 影响

- 白打一次必然失败的 strict 调用（计费 + 延迟）；
- 排障时误判为「模型能力不足」而非「调用方 schema 不兼容 strict」。

### 根因

strict 请求未对 `additionalProperties: true` 做归一处理——strict 模式要求每个 object 节点 `additionalProperties: false`（递归），显式 true 直接 400。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI structured outputs | strict 模式要求**递归每个 object 节点** `additionalProperties: false`；显式 true 报 400（`additionalProperties is required to be supplied and to be false`） |
| LangChain | `_recursive_set_additional_properties_false`：strict 模式下递归把已设置的 `additionalProperties` 强制为 false（修复 Pydantic 2.11 的 dict/Any 生成 true） |
| trusty_review / ReqLLM | `enforce_strict_mode` 递归归一 + `required` 补全；strict 可配置开关 |

**核心**：strict 请求前对 schema 递归归一 `additionalProperties: true → false`（对齐 LangChain）。

---

## 修复方案（含决策取舍）

**决策**：新增 `_strict_compliant(schema)`——递归把 `additionalProperties: true` 归一为 false 的**副本**，`_build_json_schema_request` 用它构建 strict 请求；本地校验仍用原 schema（保留调用方「允许扩展」意图）。

**取舍理由**：

1. 对齐工业界主流（LangChain 递归归一）；
2. 分离「strict 请求的 schema」与「本地校验的 schema」——strict 下模型被约束不输出额外字段（归一 false 是真实行为），本地校验按原 schema（true 允许扩展）不误判；
3. 返回副本，不污染调用方 schema（与 `_enforce_no_extra_fields` 的深拷贝风格一致）。

**语义边界**：

- `additionalProperties` 缺省或 false → 不变（`_enforce_no_extra_fields` 已补 false）；
- 显式 true → strict 请求归一 false，本地校验保持 true；
- strict 不支持「允许扩展」是固有限制，归一是 strict 下最接近的语义（对齐 LangChain）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | 新增 `_strict_compliant`（递归 true→false 副本）；`_build_json_schema_request` 用它构建 strict 请求 | `test_generate_structured.py` 新增 `test_strict_schema_normalizes_additional_properties_true` |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表加 LLM-009 条目） | — |

---

## 验证

- `tests/unit/test_generate_structured.py` **49 passed**（含新增 strict 归一用例）
- 全量测试 **362 passed**（43.46s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **strict 模式对 schema 有硬性约束**（每个 object 节点 `additionalProperties: false` + `required` 补全）：请求前必须递归归一，否则必然 400 且易被误归因为「模型不支持」。
- **请求 schema 与校验 schema 分离**：strict 请求用归一副本（约束模型），本地校验用原 schema（尊重调用方意图）——两套语义各归其位。
