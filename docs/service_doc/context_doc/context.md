# ContextManager 上下文管理说明文档

> **更新日期**：2026-08-04
> **模块**：`app/services/context_manager.py`
> **文档定位**：ContextManager 独立说明 —— 从会话历史组装 messages、tiktoken 精确计数、超限截断。

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

ContextManager 是多轮对话系统的**上下文调度器**，位于服务层，是 Chat 链路中「拿到会话 → 组装请求」的关键一步，负责：

1. **消息组装**：从会话历史提取消息，拼接成 LLM 可接受的 messages 格式
2. **Token 精确控制**：用 tiktoken 逐条计算消息与总上下文的 token 消耗，确保不超模型限制
3. **窗口管理**：超出可用窗口时，从最早的历史消息开始丢弃
4. **成本核算基础**：为每次请求提供 token 数据，供计费与监控

### 依赖关系

```text
SessionManager（get_session / get_messages 提供原始数据）
        │
        ▼
ContextManager（组装 + 计数 + 截断）
        │
        ▼
TaskService.run_agent() → Agent → LLMService（消费组装好的 messages）
```

- 唯一外部依赖是 `SessionManager`（构造参数注入），不直接接触 Redis / DB
- 上游调用方：`app/api/routes/chat.py`（`POST /api/chat/send` 第 3 步构建上下文）

### 构造参数

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `session_manager` | 必填 | `AppState` 注入 | 会话数据来源 |
| `model_name` | `"gpt-4"` | `settings.llm_model_id` | 用于解析 tiktoken encoder |
| `max_context_tokens` | `128000` | `settings.max_context_tokens` | 上下文 token 上限 |
| `max_output_tokens` | `4096` | `settings.max_output_tokens` | 输出 token 预算 |

---

## 核心类与方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `count_tokens` | `(text: str) -> int` | 用 tiktoken 编码器精确计算文本 token 数 |
| `count_messages_tokens` | `(messages: list[dict]) -> int` | 计算 messages 列表总 token：每条消息 +4 格式开销、`name` 额外 +1、末尾 +2 回复开销 |
| `build_messages` | `(session_id, user_message, max_rounds=20) -> tuple[list[dict], int]` | 组装完整 messages，超限自动截断，返回 `(messages, total_tokens)` |
| `_truncate_messages` | `(messages, max_tokens) -> list[dict]` | 保留 system prompt 与最近对话，丢弃最早历史直到不超限 |

---

## 关键实现详解

### tiktoken 编码器解析

```python
try:
    self.encoder = tiktoken.encoding_for_model(model_name)
except KeyError:
    self.encoder = tiktoken.get_encoding("cl100k_base")
```

- 按 `model_name` 解析对应编码器；未知模型抛 `KeyError` 时回退 `cl100k_base`
- 编码器在 `__init__` 中解析一次并缓存为实例属性，`count_tokens` 不再重复解析

### Token 计数规则

`count_messages_tokens` 沿用 OpenAI 官方 messages token 估算规则：

- 每条消息固定 **+4** 格式开销
- 消息内容经 `count_tokens` 精确计算
- 消息带 `name` 字段额外 **+1**
- 整体末尾固定 **+2** 回复格式开销

### `build_messages` 组装策略

```text
build_messages(session_id, user_message, max_rounds=20)
  1. get_session(session_id) → 未找到抛 ValueError("Session ... not found")
  2. get_messages(session_id, limit=max_rounds * 2)   # 每轮 user + assistant，最多 max_rounds 轮
  3. messages = [system] + history + [user]
  4. total_tokens = count_messages_tokens(messages)
     available_tokens = max_context_tokens - max_output_tokens
     if total_tokens > available_tokens:
         messages = _truncate_messages(messages, available_tokens)
         total_tokens = count_messages_tokens(messages)
  5. 返回 (messages, total_tokens)
```

- **保留策略**：system prompt 始终保留在 `messages[0]`，用户最新输入始终追加在末尾
- **截断窗口**：`available_tokens = max_context_tokens - max_output_tokens`，为输出预留预算

### `_truncate_messages` 截断逻辑

```text
_truncate_messages(messages, max_tokens)
  truncated = [messages[0]]                        # 保留 system prompt
  for msg in reversed(messages[1:-1]):            # 从最早的历史开始，去掉 system 和最后的 user
      candidate = [system] + [msg] + 已保留历史 + [user]
      if count_messages_tokens(candidate) <= max_tokens:
          truncated.insert(1, msg)                 # 能放则保留
      else:
          break                                    # 放不下则丢弃更早的
  truncated.append(messages[-1])                   # 补上最后 user 消息
```

- **迭代方向**：`reversed(messages[1:-1])` 从最近的历史往前尝试，最早的历史最先被丢弃
- **裁剪粒度**：按「整条消息」丢弃（非按 token 截断），`truncated` 始终保持 `[system] + 最近历史 + [user]` 的形态
- **已知局限**（文件注释原话）：从最早的消息开始丢弃——若早期消息包含关键信息，被丢弃后模型可能无法理解上下文。文件内提示后续可评估「历史摘要压缩」（docstring 提到，但当前未实现）

### 边缘情况

- `session_id` 不存在：`build_messages` 抛 `ValueError`（不静默降级）
- `max_rounds` 传小值时，`get_messages` 的 `limit = max_rounds * 2` 可能为 0——此时只取 system + 当前 user
- 截断只作用于历史；即使截断后仍超限（如单条 user 消息本身超长），**不会抛错**，超限部分依赖 LLM 侧容忍或服务端错误

---

## 使用示例

```python
# 构建上下文（Chat 路由核心用法，见 app/api/routes/chat.py）
messages, total_tokens = await app_state.context_manager.build_messages(
    session_id=session_id,
    user_message="继续分析不良数据",
    max_rounds=20,
)
# messages → [{"role": "system", "content": ...}, {"role": "user", "content": ...}, ...]
# total_tokens → 本次请求的预估 token 数

# token 精确计数（存消息时用于记录 token_count）
count = app_state.context_manager.count_tokens("分析这批不良率")
```

---

## 配置关联

相关配置集中在 `app/config/settings.py`（详见 [config 文档](../../config.md)）：

| 配置项 | 默认值 | 使用位置 | 说明 |
| --- | --- | --- | --- |
| `max_context_tokens` | `128000` | `build_messages` 截断窗口上限 | 上下文 token 上限 |
| `max_output_tokens` | `4096` | `available_tokens = context - output` | 为输出预留的 token 预算 |
| `max_history_rounds` | `20` | — | 配置存在，但 `build_messages` 用**参数默认值** `max_rounds=20`，未读取此配置 |
| `llm_model_id` | `gpt-4` | `model_name` 构造参数 | 决定 tiktoken 编码器 |

> **注意**：`max_history_rounds` 与 `build_messages` 的 `max_rounds` 参数默认值相同（20），但当前实现并未将配置绑定到方法参数——`AppState.initialize()` 构造 `ContextManager` 时也未传 `max_history_rounds`。

---

## 相关文档

- [服务层总览](../service.md)（ContextManager 在服务层的定位）
- [SessionManager 会话管理](../session_doc/session.md)（数据来源：`get_session` / `get_messages`）
- [LLM 层说明](../llm_doc/llm.md)（messages 的下游消费方）
- [TaskService 任务调度](../task_doc/task.md)
- [API 模块](../../api_doc/api.md)（`chat.py` 路由，本模块上游调用方）
- [核心层说明](../../core_doc/core.md)
- [架构设计](../../architecture.md)
- [配置说明](../../config.md)
- [HANDOFF](../../HANDOFF.md)
