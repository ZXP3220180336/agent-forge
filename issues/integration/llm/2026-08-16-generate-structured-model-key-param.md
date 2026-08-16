# generate_structured 参数名契约：实现签名 `model_key`，调用方误用 `model` 运行时 TypeError

> **状态**：✅ 已修复
> **优先级**：P3（低，API 使用契约问题——历史上曾导致运行时 TypeError）
> **来源**：2026-08-16 从 llm.md「常见问题」提取归档
> **涉及模块**：`app/integration/llm/llm_service.py`（`generate_structured`）· `app/integration/llm/structured.py`（`StructuredOutput.extract`）
> **关联文档**：[llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## 问题描述

### 现象

调用 `generate_structured` 时传 `model="fast"` 报 TypeError——实现签名用的是 `model_key`（`generate_structured(messages, schema, model_key="fast")` / `StructuredOutput.extract(model_key=...)`），调用方按直觉传 `model` 导致形参不匹配。

### 影响

运行时 TypeError，结构化输出调用失败（契约不清晰，调用方易踩坑）。

### 根因

历史演进中统一结构化输出入口（`generate_structured` 委托 `StructuredOutput.extract`）时，`extract` 形参名定为 `model_key`（与 `ClientManager` 配置键一致），但调用方/文档示例曾用 `model`——形参名契约未在入口处对齐或提示。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 形参名即契约 | 公共 API 形参名是调用契约的一部分——与内部配置键一致（`model_key`），并在文档/类型提示上明确；改名需同步所有调用点并加回归测试 |

---

## 修复方案（含决策取舍）

**决策**：统一以 `model_key` 为形参名（`generate_structured` 与 `extract` 一致），文档示例与 FAQ 明确「调用时须传 `model_key=model_key`」。

**修复要点**：

1. `extract` / `generate_structured` 形参统一为 `model_key`（当前实现已如此）；
2. 文档示例与 FAQ 标注正确形参名，避免调用方再传 `model`。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/llm_service.py` / `app/integration/llm/structured.py` | 形参统一为 `model_key` | 既有 `test_generate_structured.py` 用例覆盖 `model_key=` 调用 |
| `docs/integration_doc/llm_doc/llm.md` | FAQ 标注正确形参名 | — |

---

## 验证

- `generate_structured(..., model_key="fast")` 正常返回；传 `model=` 抛 TypeError（形参契约清晰）
- 全量测试通过

---

## 教训沉淀

- **公共 API 形参名是调用契约**：统一入口后形参名与配置键对齐（`model_key`），文档示例必须同步——否则调用方按直觉传 `model` 即踩 TypeError。
