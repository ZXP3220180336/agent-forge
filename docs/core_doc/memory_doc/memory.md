# 记忆系统说明文档

> **更新日期**：2026-08-03
> **模块**：`app/core/memory/` + `app/services/memory_service.py`
> **实现状态**：❌ 预留（全部文件为空）
> **架构定位**：为 Agent 提供跨会话的记忆能力，让 Agent "永不遗忘过往"

---

## 📋 目录

- [模块概述](#模块概述)
- [实现状态总览](#实现状态总览)
- [规划结构](#规划结构)
- [与产品方向的关系](#与产品方向的关系)
- [相关文档](#相关文档)

---

## 模块概述

记忆系统为 Agent 提供**跨会话的记忆能力**，分为三层：

- **短期记忆**：当前对话上下文（`MEMORY_MAX_SHORT_TERM`）
- **长期记忆**：跨会话持久化知识（向量检索，`MEMORY_VECTOR_DB`）
- **工作记忆**：当前任务进行中的临时状态

```
Agent 层
    ↓
MemoryService（服务层，对外入口）  ← 预留
    ├── short_term  短期记忆（对话内）
    ├── long_term   长期记忆（向量库，跨会话）
    └── working     工作记忆（任务中）
```

---

## 实现状态总览

| 文件 | 状态 | 定位 |
| --- | --- | --- |
| `app/services/memory_service.py` | ❌ 空 | 记忆服务对外入口 |
| `app/core/memory/__init__.py` | ❌ 空 | 子包入口 |
| `app/core/memory/base.py` | ❌ 空 | 记忆基类 |
| `app/core/memory/short_term.py` | ❌ 空 | 短期记忆 |
| `app/core/memory/long_term.py` | ❌ 空 | 长期记忆 |
| `app/core/memory/working.py` | ❌ 空 | 工作记忆 |

**当前状态**：全部为预留空文件，`MEMORY_ENABLED` 默认 `false`。

---

## 规划结构

### `base.py` — 记忆基类

定义记忆单元的抽象接口（存取、过期、检索），供短期/长期/工作记忆实现。

### `short_term.py` — 短期记忆

- 对应 `MEMORY_MAX_SHORT_TERM`（默认 10 条）
- 当前对话上下文，随会话生命周期

### `long_term.py` — 长期记忆

- 向量检索（`MEMORY_VECTOR_DB`：milvus/qdrant/pinecone）
- 复用 `EmbeddingService` 向量化 + 向量库存储
- 跨会话沉淀（"Agent 永不遗忘过往"）

### `working.py` — 工作记忆

- 当前任务进行中的临时状态（子任务进度、中间结果）

### 依赖

```
MemoryService
    ├── EmbeddingService（向量化）
    ├── VectorStore（长期记忆存储，基础设施层预留）
    └── ContextManager（短期记忆接入上下文）
```

---

## 与产品方向的关系

在良率 RCA 场景中，记忆系统用于**沉淀历史排查经验**：
- 每次良率异常排查后，把根因结论存入长期记忆
- 后续类似 excursion 发生时，Agent 可检索历史案例加速定位
- 对应 `search_historical_rca` 工具的历史案例检索能力

---

## 相关文档

- [核心层说明](../../core_doc/core.md)
- [服务层说明](../../service_doc/service.md)
- [基础设施层说明](../../infrastructure_doc/infrastructure.md)
- [product 产品方向](../../product.md)
