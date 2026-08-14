# 核心层说明文档

> **更新日期**：2026-08-03
> **文档定位**：Agent 核心层 —— 推理循环、提示词、事件系统、记忆与推理策略。
> **实现状态**：Agent（✅）/ Events（✅）/ Prompts（✅）/ Memory（❌ 预留）/ Reasoning（❌ 预留）

---

## 📋 目录

- [模块概述](#模块概述)
- [实现状态总览](#实现状态总览)
- [Agent 模块](#agent-模块)
- [Events 事件系统](#events-事件系统)
- [Prompts 提示词](#prompts-提示词)
- [Memory 记忆系统（预留）](#memory-记忆系统预留)
- [Reasoning 推理策略（预留）](#reasoning-推理策略预留)
- [相关文档](#相关文档)

---

## 模块概述

核心层是系统的**决策与行动核心**，位于服务层（LLM/工具/会话）之上，负责：

- **Agent 推理循环**：编排 LLM 推理与工具调用的循环流程
- **提示词管理**：系统/工具/规划等场景的提示词模板
- **事件系统**：统一 SSE 事件定义，LLM 层与 Agent 层共用
- **记忆与推理**：预留短期/长期记忆与多种推理策略

```
服务层（LLMService / ToolService / SessionManager ...）
    ↓
核心层（Agent / Prompts / Events / Memory / Reasoning）  ← 本模块
    ↓
app/core/agent/executor.py  →  ReAct 循环
```

---

## 实现状态总览

| 子模块 | 文件 | 状态 | 核心内容 |
| --- | --- | --- | --- |
| Agent | base.py（201行） | ✅ | BaseAgent / AgentContext / AgentResult / AgentState |
| Agent | executor.py（240行） | ✅ | ReActAgent（ReAct 循环 + 并行工具） |
| Agent | planner.py / reasoning.py | ❌ | 预留策略 |
| Events | events.py（123行） | ✅ | 7 种 SSE 事件 + 构建函数 |
| Prompts | base.py / manager.py / templates/ | ✅ | 提示词模板 + 管理器 |
| Memory | base / short_term / long_term / working | ❌ | 预留记忆系统 |
| Reasoning | chain_of_thought / react / reflection | ❌ | 预留推理策略 |

---

## Agent 模块

Agent 是核心层的**决策与行动核心**，负责编排 LLM 推理与工具调用的循环流程：

- **策略模式**：`BaseAgent.run()` 统一入口，`_strategy_cycle()` 子类实现（ReAct 当前 / Plan-then-Execute、Reflection 预留）
- **无状态设计**：每次 `run()` 新建实例，上下文经 `AgentContext` 传入
- **ReAct 循环**：推理 → 行动 → 观察，循环直到完成或达到 `max_iterations`
- **工具并行**：`_execute_tool_calls()` 用 `asyncio.gather` 并行执行（顺序保持）

**详见** [Agent 模块详解](agent_doc/agent.md)（752 行，含 BaseAgent/ReActAgent/数据结构/SSE 事件流/最佳实践/常见问题）

---

## Events 事件系统

统一 SSE 事件定义，LLM 层与 Agent 层共用，确保事件格式一致。

| type | 产出者 | 含义 |
| --- | --- | --- |
| `reasoning` | LLM 层 | 思考 token |
| `message` | LLM 层 | 回答 token |
| `error` | LLM/Agent | 异常 |
| `tool_call` | Agent | 工具调用通知 |
| `tool_result` | Agent | 工具执行结果 |
| `done` | Agent | 完成 |
| `agent_info` | Agent | 状态信息 |

核心函数：`build_sse_event(event_type, content, **extra)` + 7 个便捷构造器。

---

## Prompts 提示词

核心层的**指令层**，为 Agent 提供系统/工具/规划等场景的提示词模板：

- `PromptManager.build_system_prompt(tools_desc)`：组装系统提示词（SYSTEM_PROMPT + 工具格式说明）
- 模板：`system.py`（系统）/ `tools.py`（工具格式）/ `planning.py`（🔶 预留规划）

**详见** [提示词模块](prompt_doc/prompts.md)

---

## Memory 记忆系统（预留）

`app/core/memory/` 全部为空文件（0 字节），规划短期/长期/工作三层记忆，为 Agent 提供跨会话能力。`MemoryService`（服务层，空文件）是对外入口，对应配置 `MEMORY_ENABLED`（默认 false）。

**详见** [记忆系统（预留）](memory_doc/memory.md)

---

## Reasoning 推理策略（预留）

`app/core/reasoning/` 全部为空文件（0 字节），规划 Chain-of-Thought / ReAct / Reflection 三种推理策略，作为 `BaseAgent._strategy_cycle()` 的候选实现。

**详见** [推理策略（预留）](reasoning_doc/reasoning.md)

---

## 相关文档

- [架构设计](../architecture.md)
- [Agent 模块详解](agent_doc/agent.md)
- [提示词模块](prompt_doc/prompts.md)
- [记忆系统（预留）](memory_doc/memory.md)
- [推理策略（预留）](reasoning_doc/reasoning.md)
- [service 模块](../service_doc/service.md)
- [config 模块](../config_doc/config.md)
