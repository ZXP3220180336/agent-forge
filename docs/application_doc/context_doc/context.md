# ContextManager 上下文管理说明文档

> **对应代码**：`app/application/context/context_manager.py`
> **更新日期**：2026-08-29
> **职责**：从会话历史组装 messages、经 `TokenCounter` 端口精确计数、超限截断；并结构实现 `ContextBudgetPort`，承担 Agent 运行中的上下文预算管理
> **状态**：✅ 已实现
> **配套**：结构实现领域端口 `ContextBudgetPort`（`app/domain/ports/context_budget.py`）；token 计量委托 `TokenCounter` 端口

---

## 📋 目录

- [ContextManager 上下文管理说明文档](#contextmanager-上下文管理说明文档)
  - [📋 目录](#-目录)
  - [定位与职责](#定位与职责)
  - [接口契约](#接口契约)
  - [行为边界](#行为边界)
  - [使用示例](#使用示例)
  - [设计决策](#设计决策)
  - [测试](#测试)
  - [相关文档](#相关文档)

---

## 定位与职责

ContextManager 是 chat 链路中「拿到会话 → 组装请求」的关键一步，负责：

1. **消息组装**：system prompt + 历史对话 + 当前用户输入，拼接为 LLM 可接受的 messages 格式
2. **Token 精确控制**：经 `TokenCounter` 端口逐条计算消息与总上下文的 token 消耗，确保不超模型限制
3. **窗口管理**：超出 `max_context_tokens - max_output_tokens` 时，从最早的历史消息开始丢弃
4. **运行中预算管理**：结构实现 `ContextBudgetPort`，在 Agent 循环中（模型每次调用前）做轮次 + token 双层护栏（横切能力，所有 Agent 模式经端口注入共享）

依赖：构造注入 `SessionManager`（会话数据）与 `TokenCounter` 端口（计数），不直接接触 Redis / DB / tiktoken。上游调用方：`app/api/routes/chat.py`（`build_messages` 组装上下文，并以 `context_budget=context_manager` 注入 `ReActAgent`）。

## 接口契约

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `count_tokens(text: str) -> int` | 同步 | 计算文本 token 数（委托 `TokenCounter` 端口） |
| `count_messages_tokens(messages: list[dict]) -> int` | 同步 | 计算 messages 总 token（委托 `TokenCounter` 端口，计数规则见 [token_counter](../../integration_doc/llm_doc/token_counter.md)） |
| `build_messages(session_id, user_message, max_rounds=20) -> tuple[list[dict], int]` | 异步 | 组装完整 messages，超限自动截断，返回 `(messages, total_tokens)` |
| `trim_messages(messages, *, max_rounds, max_tokens) -> None` | 同步 | 就地裁剪 messages 到预算内（`ContextBudgetPort` 实现，Agent 循环中模型调用前调用） |

**构造参数**：

| 参数 | 默认值 | 来源 | 说明 |
| --- | --- | --- | --- |
| `session_manager` | 必填 | `Container` 注入 | 会话数据来源 |
| `token_counter` | 必填 | `Container` 注入 | `TokenCounter` 端口实现（tiktoken 适配器） |
| `max_context_tokens` | `128000` | `settings.max_context_tokens` | 上下文 token 上限 |
| `max_output_tokens` | `4096` | `settings.max_output_tokens` | 输出 token 预算 |

**对外异常**：

| 异常 | 触发 | 调用方处理 |
| --- | --- | --- |
| `ValueError` | `build_messages` 时 `session_id` 不存在 | 按会话失效处理（`"Session {session_id} not found"`） |

## 行为边界

| 场景 | 行为 |
| --- | --- |
| `session_id` 不存在 | `build_messages` 抛 `ValueError`（不静默降级） |
| `max_rounds` 过小（`limit = max_rounds * 2` 为 0） | 只取 system + 当前 user |
| 截断后仍超限（单条 user 消息本身超长） | 不抛错，超限部分依赖 LLM 侧容忍或服务端错误 |
| `trim_messages` 两个参数均为 None | 就地列表不变（无操作） |
| token 预算内且轮次未超限 | 直接返回，无副作用 |

## 使用示例

```python
# 组装上下文（chat 路由核心用法）
messages, total_tokens = await container.context_manager.build_messages(
    session_id=session_id,
    user_message="继续分析不良数据",
    max_rounds=20,
)
# messages → [{"role": "system", ...}, {"role": "user", ...}, ...]
# total_tokens → 本次请求的预估 token 数

# 运行中上下文护栏（经 ContextBudgetPort 由 Agent 循环调用）
context_manager.trim_messages(messages, max_rounds=10, max_tokens=80000)
```

## 设计决策

- **输入侧 vs 运行中护栏分离**：`build_messages`（输入侧）负责初始组装截断；`trim_messages` 系列（运行中）负责 Agent 循环中模型调用前的**增量护栏**。两次裁剪时机不同，逻辑独立（关键逻辑示意见 `context_manager.py` 对应方法注释，完整实现以源码为准）
- **轮次 + token 双层护栏**：轮次预算保证消息数有界（assistant/tool 配对原子保留）；token 预算保证总量不超窗口——`_estimate_messages_tokens` 在 `count_messages_tokens` 基础上补偿 `tool_calls.arguments` 与 `reasoning_content` 的**低估**（按字符数 `// 4` 折算）
- **Token 计量端口化**：计数职责委托 `TokenCounter` 端口，实现 `TiktokenTokenCounter` 细节（编码器解析 / content 归一化）见 [token_counter 文档](../../integration_doc/llm_doc/token_counter.md)，本模块不接触 tiktoken
- **截断粒度**：输入侧按「整条消息」从最早历史丢弃；运行中按「轮」滑动窗口丢最旧——均保留 system/user 前缀

## 测试

- `tests/unit/test_context_manager.py`：组装 / 截断 / 运行中裁剪的行为契约

## 相关文档

- [应用层说明](../README.md)（ContextManager 的定位）
- [SessionManager 会话管理](../session_doc/session.md)（数据来源：`get_session` / `get_messages`）
- [领域层说明](../../domain_doc/README.md)（`ContextBudgetPort` 端口，Agent 消费方）
- [集成层说明](../../integration_doc/README.md)（`TokenCounter` 端口实现）
- [API 模块](../../api_doc/api.md)（`chat.py` 路由，本模块上游调用方）
- [配置说明](../../config_doc/config.md)
- [架构设计](../../architecture.md)
