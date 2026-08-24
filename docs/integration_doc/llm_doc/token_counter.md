# TiktokenTokenCounter Token 计数实现说明

> **更新日期**：2026-08-24
> **模块**：`app/integration/llm/token_counter.py`
> **文档定位**：TokenCounter 端口的集成层实现 —— tiktoken 编码器解析、content 归一化、消息计数。

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

`app/integration/llm/token_counter.py` 是 **TokenCounter 端口的集成层适配器**，同时是 tiktoken 依赖的**唯一使用点**（领域/应用层不直接接触 tiktoken）：

1. **`get_encoder(model)`**：按模型名解析 tiktoken 编码器（进程内缓存、未知模型回退 `cl100k_base`）——供 LLM 层 TPM 估算复用（单一事实源）
2. **`content_to_text(content)`**：消息 content 归一化（None / str / 多模态 list）——避免 `encode(None)` 抛 TypeError
3. **`TiktokenTokenCounter`**：实现 `TokenCounter` 端口（`count_tokens` / `count_messages_tokens`），供 `ContextManager` 构造注入

### 与其它服务的关系

```text
ContextManager（应用层）── 依赖 TokenCounter 端口（app/domain/ports/token_counter.py）
        ▲ 构造注入
TiktokenTokenCounter（本模块）── 唯一 tiktoken 使用点
        ▲ 共享底层函数
llm_service._count_prompt_tokens（别名 import get_encoder / content_to_text，TPM 限流估算）
```

### 构造参数

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `model` | 必填 | `settings.llm_model_id`（`Container` 注入） | 决定 tiktoken 编码器 |

---

## 核心类与方法

### `get_encoder(model: str) -> Any`（模块级函数）

按模型名解析 tiktoken 编码器；进程内缓存（`_encoder_cache`），未知模型回退 `cl100k_base`。
只捕获 `KeyError`：tiktoken 缺失（`ImportError`）是硬依赖损坏，自然传播 fail fast。

### `content_to_text(content: Any) -> str`（模块级函数）

归一化消息 content：`None` → 空串；`str` → 原样；多模态 list → 只取文本片段拼接；异常形状 → 空串。

### `TiktokenTokenCounter`

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(model: str)` | 构造时经 `get_encoder` 解析编码器 |
| `count_tokens` | `(text: str) -> int` | 单文本 token 数 |
| `count_messages_tokens` | `(messages: list[dict]) -> int` | 消息总 token：每条 +4 格式开销 + content token + name +1，末尾 +2 |

---

## 关键实现详解

### 计数口径

`count_messages_tokens` 沿用 OpenAI 官方 messages token 估算规则（与 ContextManager 委托计数口径一致）：

- 每条消息固定 **+4** 格式开销
- content 经 `content_to_text` 归一化后精确计数
- 消息带 `name` 字段额外 **+1**
- 整体末尾固定 **+2** 回复格式开销

> **输出余量（max_tokens）不在此口径内**：那是 TPM 限流特有估算（`llm_service._count_prompt_tokens` 额外加 max_tokens），属集成层内部实现细节，不入端口。

### content 防御

`count_messages_tokens` 内部对 content 归一化（`content_to_text`）：content 键存在但为 `None`（工具报错等场景）或为多模态 list 时，归一化为文本后计数，不抛 TypeError。

### 编码器缓存

`get_encoder` 按模型名缓存（`_encoder_cache`），同一模型重复解析返回同一对象；`llm_service._get_encoder` 以别名 import 复用同一实现。

---

## 使用示例

```python
from app.integration.llm.token_counter import TiktokenTokenCounter

counter = TiktokenTokenCounter(model="deepseek-chat")
token_count = counter.count_tokens("分析这批不良率")
msg_tokens = counter.count_messages_tokens([{"role": "user", "content": "hello"}])

# 装配根注入 ContextManager（见 app/container.py）
context_manager = ContextManager(
    session_manager=session_manager,
    token_counter=TiktokenTokenCounter(model=settings.llm_model_id),
    max_context_tokens=settings.max_context_tokens,
    max_output_tokens=settings.max_output_tokens,
)
```

---

## 配置关联

| 配置项 | 默认值 | 使用位置 | 说明 |
| --- | --- | --- | --- |
| `llm_model_id` | `gpt-4` | `TiktokenTokenCounter` 构造参数 | 决定 tiktoken 编码器 |

---

## 相关文档

- [TokenCounter 端口](../../domain_doc/README.md)（领域层契约）
- [ContextManager 上下文管理](../../application_doc/context_doc/context.md)（端口消费方）
- [LLM 服务层](llm_service.md)（`get_encoder` / `content_to_text` 复用方）
- [LLM 层说明](llm.md)
