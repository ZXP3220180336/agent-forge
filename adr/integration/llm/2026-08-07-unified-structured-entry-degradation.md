# 统一结构化输出入口 + 三级降级策略

> **状态**：✅ 已采纳
> **决策日期**：2026-08-07
> **涉及模块**：`app/integration/llm/llm_service.py`（`generate_structured`）· `app/integration/llm/structured.py`（`StructuredOutput.extract`）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## Context

- 此前结构化输出存在两个入口（`generate_structured` 内部自行处理 + `StructuredOutput.extract` 直接可调），职责重叠、语义不一——messages 语义（prompt 拼接）、降级逻辑分散两处，调用方需区分用哪个入口。
- 不同模型对结构化输出支持差异很大：最可靠的 Structured Outputs（`response_format=json_schema`）不是所有模型都支持，廉价模型（fast）可能完全不支持。
- 设计目标：结构化输出**只有一个对外入口**；能走 Structured Outputs（`response_format=json_schema`）就不只依赖 prompt 或 JSON mode；不同模型都能工作；调用方无需知道底层用了哪级（透明降级）。

## Decision

**`generate_structured` 是唯一入口，内部委托 `StructuredOutput.extract` 三级降级；`StructuredOutput` 是内部实现载体。**

- **统一前 vs 统一后**：入口数 2 → 1（`generate_structured`）；messages 语义——`extract` 接收完整 messages 透传，prompt 拼接是调用方职责，结构化模块不再假设 prompt 形状；降级逻辑从分散收敛到 `extract` 单点。
- **三级降级链**（逐级降级，兼容不同模型支持度）：
  - 第一级：原生 JSON Schema（strict=True）——可靠性最高，模型要求最高
  - 第二级：JSON Mode（json_object）——只保证可解析，不保证 Schema
  - 第三级：纯 Prompt + 正则提取（无 schema）——兼容所有模型，可靠性最低
- **模式一致性**：与 `RetryHandlerManager` 等「统一入口 + 内部实现」模式一致——调用方只面对 `LLMService`，不直接触碰内部组件。

## Consequences

- **正面**：单入口（调用方永远用 `generate_structured`）；降级链单点维护——未来加错误感知重试 / Schema 校验只改 `extract` 一处；廉价模型（fast）也能产出结构化输出。
- **负面**：降级 = 多次模型调用——每级失败多一次调用，加上错误回喂（`_REASK_MAX_RETRIES=2`）每级最多 2 次回喂，三级全失败最多 7 次调用（token 消耗放大）。这是「兼容所有模型 + 错误感知重试」的**显式代价**——换取廉价模型可用性与纠错能力，而非默认接受解析失败。
