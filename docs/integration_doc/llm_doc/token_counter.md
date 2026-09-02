# TiktokenTokenCounter Token 计数实现说明

> **更新日期**：2026-09-02
> **模块**：`app/integration/llm/token_counter.py`
> **文档定位**：LLM 模块内部 tiktoken 计数组件（经 `LLMService.count_tokens` / `count_messages_tokens` 对外，经 `LLMGateway` 端口接入）——tiktoken 编码器解析、content 归一化、消息计数。
> **状态**：✅ 已实现

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

`app/integration/llm/token_counter.py` 是 **LLM 模块内部 tiktoken 计数组件**（不对外暴露端口），tiktoken 依赖的**唯一使用点**（领域/应用层不直接接触 tiktoken）：

1. **`get_encoder(model)`**：按模型名解析 tiktoken 编码器（进程内缓存、未知模型回退 `cl100k_base`）——供 LLM 层 TPM 估算复用（单一事实源）
2. **`content_to_text(content)`**：消息 content 归一化（None / str / 多模态 list）——避免 `encode(None)` 抛 TypeError
3. **`TiktokenTokenCounter`**：tiktoken 计数实现（`count_tokens` / `count_messages_tokens`），被 `LLMService` 惰性委托（`count_*` 经 `LLMGateway` 端口对外，消费方 `ContextManager` 经端口接入）

### 与其它服务的关系

```text
ContextManager（应用层）── 依赖 LLMGateway 端口（count_tokens / count_messages_tokens）
        ▲ 结构实现
LLMService（Facade）── 惰性委托
        ▲ 内部
TiktokenTokenCounter（本模块）── 唯一 tiktoken 使用点
        ▲ 共享底层函数
llm_service._count_prompt_tokens（别名 import get_encoder / content_to_text，TPM 限流估算）
```

### 构造参数

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `model` | 必填 | 主模型（`ClientManager.get_model("main")`，`LLMService` 惰性构建时解析） | 决定 tiktoken 编码器 |

---

## 核心类与方法

### `get_encoder(model: str) -> tiktoken.Encoding`（模块级函数）

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

> **输出余量（max_tokens）不在此口径内**：那是 TPM 限流特有估算（`llm_service._count_prompt_tokens` 额外加 max_tokens），属集成层内部实现细节，不属对外接口。

### content 防御

`count_messages_tokens` 内部对 content 归一化（`content_to_text`）：content 键存在但为 `None`（工具报错等场景）或为多模态 list 时，归一化为文本后计数，不抛 TypeError。

### 编码器缓存

`get_encoder` 按模型名缓存（`_encoder_cache`），同一模型重复解析返回同一对象；`llm_service._get_encoder` 以别名 import 复用同一实现。

---

## 使用示例

```python
# 直接使用底层组件（仅测试 / 集成层内部）
from app.integration.llm.token_counter import TiktokenTokenCounter

counter = TiktokenTokenCounter(model="deepseek-chat")
token_count = counter.count_tokens("分析这批不良率")
msg_tokens = counter.count_messages_tokens([{"role": "user", "content": "hello"}])

# 消费方（ContextManager 等）经 LLMGateway 端口访问（装配根注入 LLMService）
context_manager = ContextManager(
    session_manager=session_manager,
    llm=llm_service,  # LLMService.count_tokens / count_messages_tokens 委托本组件
    max_context_tokens=settings.max_context_tokens,
    max_output_tokens=settings.max_output_tokens,
)
```

---

## 配置关联

| 配置项 | 默认值 | 使用位置 | 说明 |
| --- | --- | --- | --- |
| `llm_model_id` | `gpt-4` | 主模型（`LLMService` 惰性构建 `TiktokenTokenCounter` 时） | 决定 tiktoken 编码器 |

---

## 相关文档

- [LLMGateway 端口](../../domain_doc/ports_doc/ports.md)（`count_tokens` / `count_messages_tokens` 对外契约）
- [ContextManager 上下文管理](../../application_doc/context_doc/context.md)（计数消费方）
- [LLM 服务层](llm_service.md)（`get_encoder` / `content_to_text` 复用方）
- [LLM 层说明](llm.md)
