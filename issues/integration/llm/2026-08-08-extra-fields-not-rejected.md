# 额外字段不拒绝，模型可扩展接口混入业务不需要字段

> **状态**：✅ 已修复（2026-08-08）
> **优先级**：P2（低）
> **来源**：2026-08-07 structured 模块审核（问题 4）· 2026-08-16 从 structure.md 提取归档
> **涉及模块**：`app/integration/llm/structured.py`（`extract` / `_enforce_no_extra_fields`）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md)

---

## 问题描述

### 现象

调用方 schema 若未写 `additionalProperties:false`，模型可能扩展字段混入系统（如业务不需要的 `user_emotion`）。

### 影响

模型自作主张扩展接口，混入业务不需要的字段——接口契约被打破，下游按字段消费时可能踩到意外数据。

### 根因

`schema` 未递归补 `additionalProperties:false`，默认允许额外字段。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| JSON Schema 规范 | `additionalProperties:false` 拒绝额外字段；配合 Pydantic `extra="forbid"` 双保险 |
| strict 模式 | strict JSON Schema 要求每个 object 节点 `additionalProperties:false`（递归）——本模块 LLM-009 已处理 strict 归一 |

---

## 修复方案（含决策取舍）

**决策**：`extract` 入口对 schema 深拷贝并递归补全 `additionalProperties:false`，默认拒绝额外字段。

**修复要点**：

- **`_enforce_no_extra_fields`**：深拷贝（不污染调用方 schema）+ 递归每个 object 节点补 `additionalProperties:false`；
- **显式尊重**：调用方已写 `additionalProperties:true` 的保持 true（不覆盖显式允许扩展的意图）；
- **效果**：模型无法扩展接口混入业务不需要字段，本地 `_validate_schema` / `_collect_schema_errors` 校验拒绝额外字段。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | `extract` 入口调 `_enforce_no_extra_fields` 递归补全 | `test_generate_structured.py` 额外字段拒绝用例 |

---

## 验证

- 默认拒绝额外字段，显式 `true` 尊重；本地校验拒绝
- 全量测试通过（2026-08-08 修复时验证）

---

## 教训沉淀

- **接口契约要默认关闭扩展**：`additionalProperties:false` 是工业默认，模型不应自行扩展接口混入字段；配合 Pydantic `extra="forbid"` 双保险。
- **显式意图要尊重**：调用方显式写 `true`（允许扩展）时保持——不覆盖明确意图。
