# Embedding 端口化：EmbeddingService 结构实现 EmbeddingPort（领域层嵌入抽象落地）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-24
> **涉及模块**：`app/domain/ports/embedding_port.py` · `app/integration/embedding/embedding_service.py`
> **关联文档**：[embedding.md](../../../docs/integration_doc/embedding_doc/embedding.md) · [architecture.md](../../../docs/architecture.md)

---

## Context

- 架构文档端口层目标：`EmbeddingPort（嵌入抽象）`，此前 ⬜ 未实现；`EmbeddingService` 标「✅ 已实现（孤儿）」——集成层服务，零调用方。
- 产品驱动：Phase D 主链路（Yield RCA 证据链的 `search_historical_rca` RAG）需要向量化，领域层 `YieldRcaService` 将依赖 `EmbeddingPort` 抽象。

## Decision

**定义 `EmbeddingPort` 端口（`app/domain/ports/embedding_port.py`），`EmbeddingService` 结构实现之（不显式继承）。**

1. **端口契约**：`@runtime_checkable Protocol`，`embed(text, model=None)` / `embed_batch(texts, model=None)` 两个方法。
2. **结构实现**（不继承 Protocol）：与 LLMService / TiktokenTokenCounter 同模式——方法签名已匹配端口，仅补 docstring 标注；构造依赖 `AsyncOpenAI` 保持（集成层允许直接依赖具体 client）。
3. **container 不改**：已装配（`ClientManager.get_client("main")` 作 client）。
4. **RAG 接线明确不做**：`search_historical_rca` 向量化、VectorStorePort、memory 接线均属 Phase D。
5. **工业级参照**：Clean Architecture Port/Adapter——领域层依赖抽象，向量化实现可替换（不同 embedding provider）。

## Consequences

- **正面**：领域层获得嵌入抽象（Phase D RAG 前置）；EmbeddingService 有测试覆盖（9 用例，从孤儿到已测）；依赖倒置方向正确。
- **负面**：EmbeddingService 仍零运行时消费方（端口化是铺路，非即时价值）；AsyncOpenAI 具体依赖仍在集成层（合规但未抽象到 provider 层——Phase D 按需演进）。
