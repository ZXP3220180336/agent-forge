# 记忆系统说明文档（预留）

> **对应代码**：`app/domain/memory/`
> **更新日期**：2026-08-29
> **实现状态**：⬜ 预留（全部文件为空）
> **文档定位**：记忆系统——为 Agent 提供跨会话记忆能力。当前为预留模块，全部代码为空、无对外接口、无测试；本文档只记录预留设计要点，实现后同步更新

---

## 📋 目录

- [记忆系统说明文档（预留）](#记忆系统说明文档预留)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
  - [实现状态总览](#实现状态总览)
  - [预留设计要点](#预留设计要点)
  - [与产品方向的关系](#与产品方向的关系)
  - [相关文档](#相关文档)

---

## 模块概述

记忆系统为 Agent 提供**跨会话的记忆能力**。当前全部为预留空文件，`MEMORY_ENABLED` 默认 `false`，不参与运行。

预留设计为**三层记忆** + 服务入口：

- **短期记忆**：当前对话上下文（对应 `MEMORY_MAX_SHORT_TERM`）
- **长期记忆**：跨会话持久化知识（向量检索，对应 `MEMORY_VECTOR_DB` / `MEMORY_COLLECTION`）
- **工作记忆**：当前任务进行中的临时状态（子任务进度、中间结果）
- **MemoryService**（`memory_service.py`）：对外入口，对应 `MEMORY_ENABLED`

## 实现状态总览

| 文件 | 状态 | 预留定位 |
| --- | --- | --- |
| `app/domain/memory/memory_service.py` | ⬜ 空 | 记忆服务对外入口 |
| `app/domain/memory/base.py` | ⬜ 空 | 记忆单元抽象基类 |
| `app/domain/memory/short_term.py` | ⬜ 空 | 短期记忆 |
| `app/domain/memory/long_term.py` | ⬜ 空 | 长期记忆 |
| `app/domain/memory/working.py` | ⬜ 空 | 工作记忆 |
| `app/domain/memory/__init__.py` | ⬜ 空 | 子包入口 |

## 预留设计要点

> 以下为**预留设计**，代码未实现。落地时以实际代码为准并同步更新本文档。

- **base.py**：记忆单元的抽象接口（存取 / 过期 / 检索），供三层记忆实现
- **short_term.py**：随会话生命周期，容量 `MEMORY_MAX_SHORT_TERM`（默认 10 条）；接入方式预留为经 ContextManager 并入上下文
- **long_term.py**：向量检索（`MEMORY_VECTOR_DB`：milvus / qdrant / pinecone，`MEMORY_COLLECTION` 默认 `agent_memory`），复用 `EmbeddingService` 向量化 + 向量库存储，跨会话沉淀
- **working.py**：任务进行中的临时状态
- **规划依赖**：MemoryService → EmbeddingService（向量化）+ 向量库（基础设施层预留）+ ContextManager（短期记忆接入上下文）

## 与产品方向的关系

在良率 RCA 场景中，记忆系统用于**沉淀历史排查经验**：

- 每次良率异常排查后，把根因结论存入长期记忆
- 后续类似 excursion 发生时，Agent 可检索历史案例加速定位
- 对应 `search_historical_rca` 工具的历史案例检索能力

## 相关文档

- [领域层说明](../README.md)
- [应用层说明](../../application_doc/README.md)（ContextManager 短期记忆接入）
- [Embedding 服务](../../integration_doc/embedding_doc/embedding.md)（长期记忆向量化依赖）
- [基础设施层说明](../../infrastructure_doc/infrastructure.md)（向量库预留）
- [product 产品方向](../../product.md)
