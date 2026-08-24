# 领域层说明文档

> **更新日期**：2026-08-24
> **文档定位**：领域层（`app/domain/`）—— Agent 内核、提示词、记忆与推理策略；事件系统位于共享层 `app/shared/events.py`（见 [events.md](../shared_doc/events.md)）。
> **实现状态**：Agent（✅）/ Prompts（🔶 待补测试）/ Memory（❌ 预留）/ Reasoning（❌ 预留）

---

## 📋 目录

- [模块概述](#模块概述)
- [实现状态总览](#实现状态总览)
- [Agent 模块](#agent-模块)
- [领域端口契约](#领域端口契约)
- [Prompts 提示词](#prompts-提示词)
- [Memory 记忆系统（预留）](#memory-记忆系统预留)
- [Reasoning 推理策略（预留）](#reasoning-推理策略预留)
- [相关文档](#相关文档)

---

## 模块概述

领域层是系统的**决策与行动核心**，位于应用/集成层（LLM/工具/会话）之上，负责：

- **Agent 推理循环**：编排 LLM 推理与工具调用的循环流程
- **提示词管理**：系统/工具/规划等场景的提示词模板
- **事件系统**：统一 SSE 事件定义，LLM 层与 Agent 层共用（共享层 `app/shared/events.py`，见 [events.md](../shared_doc/events.md)）
- **记忆与推理**：预留短期/长期记忆与多种推理策略

```
应用/集成层（LLMService / ToolService / SessionManager ...）
    ↓
领域层（Agent / Prompts / Events / Memory / Reasoning）  ← 本模块
    ↓
app/domain/agent/executor.py  →  ReAct 循环
```

---

## 实现状态总览

| 子模块 | 文件 | 状态 | 核心内容 |
| --- | --- | --- | --- |
| Agent | base.py（202行） | ✅ | BaseAgent / AgentContext / AgentResult / AgentState |
| Agent | executor.py（267行） | ✅ | ReActAgent（ReAct 循环 + 并行工具） |
| Agent | planner.py / reasoning.py | ❌ | 预留策略 |
| Prompts | base.py / manager.py / templates/ | 🔶 | 提示词模板 + 管理器（待补测试） |
| Memory | base / short_term / long_term / working | ❌ | 预留记忆系统 |
| Reasoning | chain_of_thought / react / reflection | ❌ | 预留推理策略 |

---

## Agent 模块

Agent 是领域层的**决策与行动核心**，负责编排 LLM 推理与工具调用的循环流程：

- **策略模式**：`BaseAgent.run()` 统一入口，`_strategy_cycle()` 子类实现（ReAct 当前 / Plan-then-Execute、Reflection 预留）
- **无状态设计**：每次 `run()` 新建实例，上下文经 `AgentContext` 传入
- **ReAct 循环**：推理 → 行动 → 观察，循环直到完成或达到 `max_iterations`
- **工具并行**：`_execute_tool_calls()` 用 `asyncio.gather` 并行执行（顺序保持）

**详见** [Agent 模块详解](agent_doc/agent.md)（754 行，含 BaseAgent/ReActAgent/数据结构/SSE 事件流/最佳实践/常见问题）

---

## 领域端口契约

领域端口定义领域层对能力层的抽象契约（依赖倒置，装配根 `container.py` 注入集成层实现）：`LLMGateway` / `StreamResult` / `TokenCounter` / `EmbeddingPort` / `ToolGateway` / `ToolResult`（见 `app/domain/ports/__init__.py`）。

### LLM Gateway

`app/domain/ports/llm_gateway.py` 定义领域层对 LLM 调用的抽象契约：

- **`LLMGateway`（Protocol）**：`async_generate`（流式）/ `generate`（非流式）/ `generate_structured`（结构化）三个方法签名
- **`StreamResult`**（单轮 LLM 结果载体）：

| 字段 | 说明 |
| --- | --- |
| `content` / `reasoning_content` | 回复 / 推理文本 |
| `finish_reason` / `tool_calls` / `usage` / `refusal` | 停止原因 / 工具调用 / Token 用量 / 拒答 |
| `error` | LLM 调用失败原因（create 失败 / 流中断放弃 / 用户取消），`None`=成功；供 `ReActAgent` 短路决策，避免把「失败」当「空输出」空转重试。正常空回（stop + 空 content）不置位（LLM-001） |

**详见** [LLM 层说明](../integration_doc/llm_doc/llm.md) · [问题文档 LLM-001](../../issues/integration/llm/2026-08-16-stream-error-propagation.md)

### TokenCounter

`app/domain/ports/token_counter.py` 定义领域层对 token 计量的抽象契约（应用层 `ContextManager` 依赖，集成层 `TiktokenTokenCounter` 结构实现）：

- **`TokenCounter`（Protocol）**：`count_tokens(text)`（单文本）/ `count_messages_tokens(messages)`（消息列表，含格式开销）两个方法签名

**详见** [token_counter 实现](../integration_doc/llm_doc/token_counter.md)

### EmbeddingPort

`app/domain/ports/embedding_port.py` 定义领域层对文本向量化的抽象契约（集成层 `EmbeddingService` 结构实现）：

- **`EmbeddingPort`（Protocol）**：`embed(text, model=None)`（单文本）/ `embed_batch(texts, model=None)`（批量，自动分批保序）两个方法签名

**详见** [embedding_service 实现](../integration_doc/embedding_doc/embedding.md)

### ToolGateway

`app/domain/ports/tool_gateway.py` 定义领域层对工具执行的抽象契约（集成层 `ToolService` 结构实现）：

- **`ToolGateway`（Protocol）**：`get_openai_tools()`（工具 Schema 导出）/ `execute(name, parameters, timeout=None, max_retries=None, retry_delay=1.0)`（执行，返回 `ToolResult`）
- **`ToolResult`**（工具执行结果载体）：`success` / `content` / `error` / `error_code`（`ErrorCode` 枚举）/ `metadata` / `execution_time` / `retry_count`

**详见** [工具模块接口](../integration_doc/tools_doc/tools.md)

---

## Prompts 提示词

领域层的**指令层**，为 Agent 提供系统/工具/规划等场景的提示词模板：

- `PromptManager.build_system_prompt(tools_desc)`：组装系统提示词（SYSTEM_PROMPT + 工具格式说明）
- 模板：`system.py`（系统）/ `tools.py`（工具格式）/ `planning.py`（🔶 预留规划）

**详见** [提示词模块](prompts_doc/prompts.md)

---

## Memory 记忆系统（预留）

`app/domain/memory/` 全部为空文件（0 字节），规划短期/长期/工作三层记忆，为 Agent 提供跨会话能力。`MemoryService`（服务层，空文件）是对外入口，对应配置 `MEMORY_ENABLED`（默认 false）。

**详见** [记忆系统（预留）](memory_doc/memory.md)

---

## Reasoning 推理策略（预留）

`app/domain/reasoning/` 全部为空文件（0 字节），规划 Chain-of-Thought / ReAct / Reflection 三种推理策略，作为 `BaseAgent._strategy_cycle()` 的候选实现。

**详见** [推理策略（预留）](reasoning_doc/reasoning.md)

---

## 相关文档

- [架构设计](../architecture.md)
- [Agent 模块详解](agent_doc/agent.md)
- [提示词模块](prompts_doc/prompts.md)
- [记忆系统（预留）](memory_doc/memory.md)
- [推理策略（预留）](reasoning_doc/reasoning.md)
- [事件系统（共享层）](../shared_doc/events.md)
- [应用层说明](../application_doc/README.md)
- [config 模块](../config_doc/config.md)
