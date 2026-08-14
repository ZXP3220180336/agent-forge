# EmbeddingService 文本向量化说明文档

> **更新日期**：2026-08-04
> **模块**：`app/services/embedding_service.py`
> **文档定位**：EmbeddingService 独立说明 —— 文本向量化（单条 / 批量 / 内存缓存）。

---

## 📋 目录

- [模块概述](#模块概述)
- [核心类与方法](#核心类与方法)
- [关键实现详解](#关键实现详解)
- [使用示例](#使用示例)
- [配置关联](#配置关联)
- [相关文档](#相关文档)

---

## 模块概述

### 定位与职责

EmbeddingService 是系统的**文本向量化服务**，封装 OpenAI 兼容的 Embedding API，负责：

1. **单文本嵌入**：`embed(text)` 返回向量数组
2. **批量嵌入**：`embed_batch(texts)` 自动分批（每批 `max_batch_size` 条）调用 API
3. **内存缓存**：MD5 哈希键 + 模型名前缀去重，缓存仅在同一实例生命周期内有效
4. **缓存穿透优化**：只请求未命中的文本，结果保持输入顺序

### 与其它服务的关系

```text
调用方（记忆服务 / 检索模块 / 向量库写入）
        │
        ▼
EmbeddingService（embed / embed_batch / clear_cache / cache_size）
        │
        └──────► AsyncOpenAI（client.chat.completions 同 client，GET /embeddings）
```

- `AppState.initialize()` 中复用 `ClientManager.get_client("main")` 作为 client（见 [服务层总览](../service.md)）
- 是记忆服务（`MemoryService`，预留）规划中「长期记忆向量化」的候选能力（见 [记忆系统（预留）](../../core_doc/memory_doc/memory.md)）

### 构造参数

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `client` | 必填 | `ClientManager.get_client("main")` | AsyncOpenAI 实例 |
| `model` | `"text-embedding-3-small"` | `settings.llm_embedding_model_id` | 嵌入模型 |
| `dimensions` | `1536` | `settings.llm_embedding_dimensions` | 向量维度 |
| `max_batch_size` | `20` | 代码默认 | 每批最大文本数 |
| `enable_cache` | `True` | 代码默认 | 是否启用内存缓存 |

---

## 核心类与方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `embed` | `(text, model=None) -> list[float]` | 单文本嵌入（内部调 `embed_batch([text])` 取 `[0]`） |
| `embed_batch` | `(texts, model=None) -> list[list[float]]` | 批量嵌入，自动分批，结果保持输入顺序 |
| `clear_cache` | `() -> None` | 清空缓存（`enable_cache=False` 时为空操作） |
| `cache_size` | `property -> int` | 缓存条目数（未启用缓存时返回 0） |
| `_make_cache_key` | `(text, model) -> str`（staticmethod） | 缓存键：`"{model}:{md5(text)}"` |

---

## 关键实现详解

### 批量流程

```text
embed_batch(texts, model)
  1. texts 为空 → 返回 []
  2. model_name = model or self._model
  3. 查缓存：
     · 命中 → results[i] = 缓存向量
     · 未命中 → 收集 uncached_indices / uncached_texts
  4. 全部命中 → 直接返回 results
  5. 未命中项按 max_batch_size 分批：
     · for each batch: client.embeddings.create(model, input=batch, dimensions)
     · 每批结果按输入顺序写回 results + 写缓存
  6. 返回 results（保持与输入相同的顺序）
```

### 缓存策略

- **缓存键**：`"{model}:{md5(text)}"` —— 模型名前缀防止跨模型向量混用，MD5 对文本取指纹
- **作用域**：`self._cache` 为实例属性（`dict`），**仅在同一实例生命周期内有效**，进程重启即失效
- **开启 / 关闭**：`enable_cache=False` 时 `self._cache = None`，查缓存 / 写缓存 / `clear_cache` 均走空操作
- **无 LRU 上限**：当前是**普通 dict 缓存**，无容量上限、无淘汰策略——长时间运行、文本变化频繁时内存可能持续增长（见「边缘情况」）

### 分批与保序

```python
for batch_start in range(0, len(uncached_texts), self._max_batch_size):
    batch = uncached_texts[batch_start : batch_start + self._max_batch_size]
    batch_indices = uncached_indices[batch_start : batch_start + self._max_batch_size]
    response = await self._client.embeddings.create(
        model=model_name, input=batch, dimensions=self._dimensions,
    )
    for j, data in enumerate(response.data):
        idx = batch_indices[j]
        results[idx] = data.embedding
```

- `results` 预分配为 `[None] * len(texts)`，按**原始下标**（`uncached_indices`）回填，天然保持输入顺序
- API 每批最多 `max_batch_size` 条，超出自动切批

### 维度控制

- 请求时显式传 `dimensions=self._dimensions`（默认 1536），与 `text-embedding-3-small` 默认一致
- 若调用方传入 `model` 覆盖模型，维度仍沿用构造时的 `dimensions`

### 边缘情况

- 缓存为**普通 dict、无上限**：`enable_cache=True`（默认）且长期运行 / 高基数文本时，内存占用会随文本种类增长（`cache_size` 属性可用于监控）
- 缓存命中判断基于**原文 MD5**：文本哪怕一字之差也视为不同，重复调用同一文本才命中
- 空输入列表直接返回 `[]`，不触发 API 调用
- 全部命中时**不调用 API**（缓存穿透优化）
- `_cache = None`（禁用缓存）时，`cache_size` 返回 0、`clear_cache` 为空操作——代码已做 None 守卫

---

## 使用示例

```python
# 单文本嵌入
vector = await app_state.embedding_service.embed("这是什么产品")
# → [0.012, -0.034, ...] 共 1536 维

# 批量嵌入（自动分批，保持输入顺序，缓存去重）
vectors = await app_state.embedding_service.embed_batch(
    ["文本一", "文本二", "文本三"],
)

# 指定模型覆盖（仍用构造时的 dimensions）
vector = await app_state.embedding_service.embed("Hello", model="text-embedding-3-small")

# 缓存操作
app_state.embedding_service.clear_cache()
print(app_state.embedding_service.cache_size)
```

---

## 配置关联

相关配置集中在 `app/config/settings.py`（详见 [config 文档](../../config_doc/config.md) 与 [LLM 层文档](../llm_doc/llm.md)）：

| 配置项 | 默认值 | 使用位置 | 说明 |
| --- | --- | --- | --- |
| `llm_embedding_model_id` | `text-embedding-3-small` | `model` 构造参数 | 嵌入模型 |
| `llm_embedding_dimensions` | `1536` | `dimensions` 构造参数 | 向量维度（必须为正数，配置校验） |

> `AppState.initialize()` 用 `ClientManager.get_client("main")` 作为 client——即嵌入请求与主模型共享连接池实例。`max_batch_size` / `enable_cache` 当前无配置项，为代码内默认值（20 / True）。

---

## 相关文档

- [服务层总览](../service.md)（EmbeddingService 在服务层的定位）
- [LLM 层说明](../llm_doc/llm.md)（ClientManager 连接池、嵌入配置分组）
- [记忆系统（预留）](../../core_doc/memory_doc/memory.md)（规划中「长期记忆向量化」的能力来源）
- [核心层说明](../../core_doc/core.md)
- [架构设计](../../architecture.md)
- [配置说明](../../config_doc/config.md)
- [HANDOFF](../../HANDOFF.md)
