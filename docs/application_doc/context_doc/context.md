# ContextManager 上下文管理说明文档

> **更新日期**：2026-08-29
> **模块**：`app/application/context/context_manager.py`
> **文档定位**：ContextManager 独立说明 —— 从会话历史组装 messages、经 `TokenCounter` 端口精确计数、超限截断；并结构实现 `ContextBudgetPort`，承担 Agent 运行中的上下文预算管理。

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

ContextManager 是 chat 链路中「拿到会话 → 组装请求」的关键一步，负责：

1. **消息组装**：system prompt + 历史对话 + 当前用户输入，拼接为 LLM 可接受的 messages 格式
2. **Token 精确控制**：经 `TokenCounter` 端口逐条计算消息与总上下文的 token 消耗，确保不超模型限制
3. **窗口管理**：超出 `max_context_tokens - max_output_tokens` 时，从最早的历史消息开始丢弃
4. **运行中预算管理**：结构实现 `ContextBudgetPort`，在 Agent 循环中（模型每次调用前）做轮次 + token 双层护栏（横切能力，所有 Agent 模式经端口注入共享）

### 依赖关系

```text
SessionManager（get_session / get_messages 提供原始数据）
        │
        ▼
ContextManager（组装 + 计数 + 截断）
        │
        ▼
ReActAgent（领域层，经 ContextBudgetPort 复用 trim_messages）→ LLMService
```

- 构造依赖 `SessionManager`（会话数据）与 `TokenCounter` 端口（token 计数），均注入传入，不直接接触 Redis / DB / tiktoken
- 结构实现 `ContextBudgetPort` 端口（`app/domain/ports/context_budget.py`）：领域层 Agent 经端口依赖本模块的 `trim_messages`（依赖倒置，横切能力注入共享）
- 上游调用方：`app/api/routes/chat.py`（`build_messages` 组装上下文，并以 `context_budget=context_manager` 注入 `ReActAgent`）

### 构造参数

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `session_manager` | 必填 | `Container` 注入 | 会话数据来源 |
| `token_counter` | 必填 | `Container` 注入 | `TokenCounter` 端口实现（tiktoken 适配器） |
| `max_context_tokens` | `128000` | `settings.max_context_tokens` | 上下文 token 上限 |
| `max_output_tokens` | `4096` | `settings.max_output_tokens` | 输出 token 预算 |

---

## 核心类与方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `count_tokens` | `(text: str) -> int` | 经 `TokenCounter` 端口精确计算文本 token 数 |
| `count_messages_tokens` | `(messages: list[dict]) -> int` | 经 `TokenCounter` 端口计算 messages 总 token（计数规则见 [token_counter](../../integration_doc/llm_doc/token_counter.md)） |
| `build_messages` | `(session_id, user_message, max_rounds=20) -> tuple[list[dict], int]` | 组装完整 messages，超限自动截断，返回 `(messages, total_tokens)`；`session_id` 不存在抛 `ValueError` |
| `trim_messages` | `(messages, *, max_rounds, max_tokens) -> None` | 就地裁剪 messages 到预算内（`ContextBudgetPort` 实现，Agent 循环中模型调用前调用） |

> `session_id` 为 `SessionId`（`app/shared/types.py` NewType）。

---

## 关键实现详解

### Token 计数端口化

token 计量职责经 `TokenCounter` 端口（`app/domain/ports/token_counter.py`）委托集成层实现 `TiktokenTokenCounter`：

- 端口契约：`count_tokens` / `count_messages_tokens` 两个方法
- 实现 `TiktokenTokenCounter`：构造时按 `model_name` 解析 tiktoken 编码器（未知模型回退 `cl100k_base`），`count_messages_tokens` 内含 content 归一化防御（None / 多模态 list 不崩溃）
- 实现细节见 [token_counter.md](../../integration_doc/llm_doc/token_counter.md)

### `build_messages` 组装策略

```text
build_messages(session_id, user_message, max_rounds=20)
  1. get_session(session_id) → 未找到抛 ValueError("Session ... not found")
  2. get_messages(session_id, limit=max_rounds * 2)   # 每轮 user + assistant
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
  for msg in reversed(messages[1:-1]):            # 从最近的历史往前尝试
      candidate = [system] + [msg] + 已保留历史 + [user]
      if count_messages_tokens(candidate) <= max_tokens:
          truncated.insert(1, msg)                 # 能放则保留
      else:
          break                                    # 放不下则丢弃更早的
  truncated.append(messages[-1])                   # 补上最后 user 消息
```

- **迭代方向**：从最近的历史往前尝试，最早的历史最先被丢弃
- **裁剪粒度**：按「整条消息」丢弃（非按 token 截断），`truncated` 始终保持 `[system] + 最近历史 + [user]` 形态
- **已知局限**（文件注释原话）：若早期消息包含关键信息，被丢弃后模型可能无法理解上下文；docstring 提到「历史摘要压缩」但当前未实现

### Agent 运行中上下文预算管理（ContextBudgetPort）

`build_messages`（输入侧）负责初始组装截断；`trim_messages` 系列（运行中）负责 Agent 循环中、模型每次调用前的**增量护栏**，两者定位互补：

- **端口**：`ContextBudgetPort`（`app/domain/ports/context_budget.py`）——领域层拥有的抽象契约，`ContextManager` 结构实现之（依赖倒置，不直接 import 应用层）
- **装配**：`chat.py` 构造 `ReActAgent(..., context_budget=context_manager)`，`ReActStrategy` 在 CONTINUE 循环末尾、模型下次调用前调用 `trim_messages`，作为上下文 gatekeeper
- **对齐工业界 turn-aware trimming**：保留 system/user 前缀 + 最近 N 轮 assistant/tool 配对消息，token 超限时逐轮丢最旧

```text
trim_messages(messages, *, max_rounds, max_tokens)   # 就地修改 messages
  1. max_rounds 不为 None → _trim_to_recent_rounds（轮次滑动窗口）
  2. max_tokens  不为 None → _trim_to_token_budget（token 超限逐轮丢最旧）
```

- **轮次护栏** `_trim_to_recent_rounds`：保留 system/user 前缀 + 最近 `max_rounds` 轮 assistant/tool **配对**消息（配对原子保留，不会出现半轮）；轮数已达标直接返回，无副作用
- **token 护栏** `_trim_to_token_budget`：经 `_estimate_messages_tokens` 估算总 token——在 `count_messages_tokens` 基础上补偿 `tool_calls.arguments` 与 `reasoning_content` 的**低估**（按字符数 `// 4` 折算），超出预算逐轮删除最旧 assistant/tool 对

**裁剪粒度对比**：

| 场景 | 方法 | 粒度 |
| --- | --- | --- |
| 输入侧组装超限 | `_truncate_messages` | 按「整条消息」丢弃最早历史，保留 system + 最近历史 + user |
| 运行中轮次超限 | `_trim_to_recent_rounds` | 按「轮」滑动窗口，配对原子保留 |
| 运行中 token 超限 | `_trim_to_token_budget` | 逐轮丢最旧 assistant/tool 对 |

### 成本上限（CostLimiter，同目录兄弟组件）

`CostLimiter` 与 `ContextManager` 同属「Agent 运行中护栏」横切能力（同 `app/application/context/`），结构实现 `CostLimiterPort`（`app/domain/ports/cost_limiter.py`）。成本估算经 **`LLMGateway.calculate_cost`**（成本估算是 LLM 能力，归属 LLM 网关端口，由 LLM 模块 Facade `LLMService` 实现）——应用层不直接 import 集成层、不触及 LLM 子组件 `CostTracker`，对齐 `ContextManager` 经 `TokenCounter` 端口先例。无状态纯函数——装配根可安全共享单例。

```text
CostLimiter(ceiling=agent_max_cost, llm=llm_service, model=llm_model_id)
  check(累计 usage) -> (exceeded, cost_usd)   # 严格 > 超限；ceiling=None 恒不超限
```

- **装配**：`container.py` 在 `agent_max_cost` 配置非 None 且 LLM 服务就绪时构造 `CostLimiter` 单例（llm 注入 `llm_service` Facade，model=`llm_model_id`），`chat.py` 经 `Depends(get_cost_limiter)` 注入 `ReActAgent(..., cost_limiter=...)`；未配置 → None，ReAct 循环成本检查零开销
- **消费**：`ReActStrategy` 每轮 usage 累加后 `check(累计 usage)`，超限走 `COST_EXCEEDED` 错误分发（默认 STOP 停机降级），见 [react.md](../../domain_doc/reasoning_doc/react.md) 成本上限节

### 边缘情况

| 场景 | 行为 |
| --- | --- |
| `session_id` 不存在 | `build_messages` 抛 `ValueError`（不静默降级） |
| `max_rounds` 传小值（`limit = max_rounds * 2` 为 0） | 只取 system + 当前 user |
| 截断后仍超限（单条 user 消息本身超长） | 不抛错，超限部分依赖 LLM 侧容忍或服务端错误 |
| `trim_messages` 两参数均为 None | 就地列表不变（无操作） |

---

## 使用示例

```python
# 构建上下文（Chat 路由核心用法，见 app/api/routes/chat.py）
messages, total_tokens = await container.context_manager.build_messages(
    session_id=session_id,
    user_message="继续分析不良数据",
    max_rounds=20,
)
# messages → [{"role": "system", "content": ...}, {"role": "user", "content": ...}, ...]
# total_tokens → 本次请求的预估 token 数

# 运行中上下文护栏（经 ContextBudgetPort 由 Agent 循环调用）
context_manager.trim_messages(messages, max_rounds=10, max_tokens=80000)
```

---

## 配置关联

相关配置集中在 `app/config/settings.py`（详见 [config 文档](../../config_doc/config.md)）：

| 配置项 | 默认值 | 使用位置 | 说明 |
| --- | --- | --- | --- |
| `max_context_tokens` | `128000` | `build_messages` 截断窗口上限 | 上下文 token 上限 |
| `max_output_tokens` | `4096` | `available_tokens = context - output` | 为输出预留的 token 预算 |
| `max_history_rounds` | `20` | — | 配置存在，但 `build_messages` 用**参数默认值** `max_rounds=20`，未读取此配置 |

> 决定 tiktoken 编码器的 `llm_model_id` 属集成层 `TiktokenTokenCounter` 配置（经端口间接影响计数），见 [token_counter.md](../../integration_doc/llm_doc/token_counter.md)。`Container.initialize()` 构造 `ContextManager` 时未传 `max_history_rounds`。

---

## 相关文档

- [应用层说明](../README.md)（ContextManager 的定位）
- [SessionManager 会话管理](../session_doc/session.md)（数据来源：`get_session` / `get_messages`）
- [领域层说明](../../domain_doc/README.md)（`ContextBudgetPort` 端口，Agent 消费方）
- [集成层说明](../../integration_doc/README.md)（`TokenCounter` 端口实现）
- [路由模块](../../api_doc/routes_doc/routes.md)（`chat.py` 路由，本模块上游调用方）
- [架构设计](../../architecture.md)
- [配置说明](../../config_doc/config.md)
