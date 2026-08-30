# 领域层说明文档

> **对应代码**：`app/domain/`
> **更新日期**：2026-08-29
> **文档定位**：领域层（`app/domain/`）—— Agent 内核、提示词、记忆与推理策略；是系统的**决策与行动核心**，只依赖领域端口与共享内核，零外部框架依赖。
> **实现状态**：Agent（✅）· Prompts（🔶 待补测试）· Reasoning（🔶 react ✅）· Memory（⬜ 预留）· Ports（✅）
> **配套**：事件系统位于共享层 `app/shared/events.py`（见 [events.md](../shared_doc/events.md)）

---

## 📋 目录

- [领域层说明文档](#领域层说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [实现状态总览](#实现状态总览)
  - [Agent 模块](#agent-模块)
  - [Prompts 提示词](#prompts-提示词)
  - [Memory 记忆系统（预留）](#memory-记忆系统预留)
  - [Reasoning 推理策略](#reasoning-推理策略)
  - [领域端口契约](#领域端口契约)
  - [典型调用链路](#典型调用链路)
  - [配置关联](#配置关联)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

领域层是系统的**决策与行动核心**，位于应用层（用例/调度）之下、集成层（LLM/工具/嵌入）之上，负责：

- **Agent 推理编排**：`BaseAgent.run()` 统一入口，编排 LLM 推理与工具调用的循环流程（策略模式）
- **推理策略实现**：`reasoning/` 提供原子推理算法（ReAct ✅ / Reflection、CoT 预留），被 agent/ 编排调用
- **提示词管理**：`prompts/` 提供系统/工具/规划等场景的提示词模板（指令层）
- **记忆能力**：`memory/` 规划短期/长期/工作三层记忆（预留）
- **端口契约**：`ports/` 定义领域层对能力层的抽象（依赖倒置，集成层实现）

### 模块结构

```text
app/domain/
├── agent/                     ← Agent 编排层（策略编排 + 生命周期）
│   ├── base.py                ← AgentState / AgentContext / AgentResult / BaseAgent
│   ├── executor.py            ← ReActAgent（桥接 reasoning/react.py 的 ReActStrategy）
│   ├── planner.py             ← PlannerAgent（Plan-then-Execute，预留）
│   └── reasoning.py           ← ReflectionAgent（预留）
├── memory/                    ← 记忆系统（预留）
│   ├── base.py / working.py / short_term.py / long_term.py / memory_service.py
├── ports/                     ← 领域端口契约（依赖倒置抽象）
│   ├── llm_gateway.py         ← LLMGateway / StreamResult（含成本估算 / Token 计量）
│   ├── tool_gateway.py        ← ToolGateway / ToolResult
│   ├── context_budget.py      ← ContextBudgetPort（上下文预算管理）
│   ├── cost_limiter.py        ← CostLimiterPort（成本上限护栏）
│   └── embedding_port.py      ← EmbeddingPort
├── prompts/                   ← 提示词管理（指令层）
│   ├── base.py                ← PromptTemplate 模板基类
│   ├── manager.py             ← PromptManager 管理器
│   └── templates/             ← system.py / tools.py / planning.py 模板
└── reasoning/                 ← 原子推理策略库
    ├── react.py               ← ReActStrategy（✅）
    ├── reflection.py          ← Reflection 策略（预留）
    └── chain_of_thought.py    ← CoT 策略（预留）
```

### 设计原则

1. **策略模式**：`BaseAgent.run()` 统一入口，`_strategy_cycle()` 抽象策略接口；子类（ReActAgent / PlannerAgent / ReflectionAgent）选择并组合推理策略
2. **编排与实现分离**：agent/ 管策略编排与生命周期，reasoning/ 管策略实现（原子推理算法）——依赖方向 `agent → reasoning`，策略层不反向依赖
3. **依赖倒置**：领域层定义端口（`LLMGateway` / `ToolGateway` / `EmbeddingPort` 等），集成层结构实现，装配根 `container.py` 注入
4. **零外部框架依赖**：领域层只依赖标准库 + `shared` + `ports`，禁止 import 集成层 / 基础设施 / 外部框架
5. **无状态设计**：Agent 每次 `run()` 新建实例，上下文经 `AgentContext` 传入，运行期间不变

### 依赖关系

```text
应用层 / API 层（task_service / chat 路由）
        ▼ 调用 run()
app/domain/
  ├── agent/ ──→ reasoning/（编排调用原子策略）
  ├── agent/ ──→ prompts/（提示词组装）
  ├── agent/ / memory/ ──→ ports/（依赖倒置：LLMGateway / ToolGateway / EmbeddingPort 等）
  └── 全部 ──→ shared/（events / exceptions / types，共享内核）
        │  端口由集成层实现
        ▼
app/integration/（LLMService / ToolService / EmbeddingService / ...）
```

---

## 实现状态总览

| 子模块 | 文件 | 状态 | 核心内容 |
| --- | --- | --- | --- |
| Agent | base.py | ✅ | BaseAgent / AgentContext / AgentResult / AgentState |
| Agent | executor.py | ✅ | ReActAgent（桥接 ReActStrategy，见 [executor.md](agent_doc/executor.md)） |
| Agent | planner.py / reasoning.py | ⬜ | PlannerAgent / ReflectionAgent（预留） |
| Prompts | base.py / manager.py / templates/ | 🔶 | 提示词模板 + 管理器（待补测试） |
| Memory | base / working / short_term / long_term / memory_service | ⬜ | 三层记忆（预留） |
| Reasoning | react.py | ✅ | ReActStrategy（见 [react.md](reasoning_doc/react.md)） |
| Reasoning | reflection / chain_of_thought | ⬜ | Reflection / CoT 策略（预留） |
| Ports | llm_gateway / tool_gateway / context_budget / cost_limiter / embedding_port | ✅ | 领域端口契约（依赖倒置，见 [ports.md](ports_doc/ports.md)） |

---

## Agent 模块

**代码**：`app/domain/agent/` · **文档**：[Agent 模块对外接口文档](agent_doc/agent.md) · [ReActAgent 桥接组件](agent_doc/executor.md)

负责编排 LLM 推理与工具调用的循环流程，是系统的**决策与行动核心**：

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| `BaseAgent` | base.py | 生命周期骨架（run/状态/事件路由/结果）+ 数据契约 | ✅ |
| `ReActAgent` | executor.py | 桥接 ReActStrategy 到 BaseAgent 生命周期 | ✅ |
| `PlannerAgent` | planner.py | Plan-then-Execute 编排（规划→执行→汇总） | ⬜ 预留 |
| `ReflectionAgent` | reasoning.py | Reflection 编排（生成→自查→修正） | ⬜ 预留 |

---

## Prompts 提示词

**代码**：`app/domain/prompts/` · **文档**：[提示词模块](prompts_doc/prompts.md)

领域层的**指令层**，为 Agent 提供系统/工具/规划等场景的提示词模板：

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| `PromptManager` | manager.py | 提示词组装入口（build_system_prompt） | 🔶 |
| `PromptTemplate` | base.py | 模板基类（format / raw） | 🔶 |
| 模板 | templates/system.py | `SYSTEM_PROMPT` 系统提示词 | 🔶 |
| 模板 | templates/tools.py | `TOOL_FORMAT_PROMPT` 工具格式提示词 | 🔶 |
| 模板 | templates/planning.py | `PLANNING_PROMPT` 规划提示词 | 🔶 draft |

---

## Memory 记忆系统（预留）

**代码**：`app/domain/memory/` · **文档**：[记忆系统](memory_doc/memory.md)

规划短期/长期/工作三层记忆，为 Agent 提供跨会话能力。`MemoryService` 是对外入口，对应配置 `MEMORY_ENABLED`（默认 false）。当前全部为预留空文件。

---

## Reasoning 推理策略

**代码**：`app/domain/reasoning/` · **文档**：[推理策略](reasoning_doc/reasoning.md) · [ReActStrategy 组件](reasoning_doc/react.md)

领域层的**原子推理策略库**，为 Agent 提供推理方式实现（被 agent/ 层编排调用）：

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| `ReActStrategy` | react.py | 推理 ↔ 工具循环算法（含工具并行原语） | ✅ |
| `ReflectionStrategy` | reflection.py | 生成 → 自查 → 修正 | ⬜ 预留 |
| CoT | chain_of_thought.py | 纯推理引导 | ⬜ 预留 |

---

## 领域端口契约

**代码**：`app/domain/ports/` · **文档**：[领域端口契约对外接口文档](ports_doc/ports.md)

领域层定义能力抽象（依赖倒置），集成层结构实现、装配根注入。契约详情（方法签名 / 结果载体 / 错误码）见 [ports.md](ports_doc/ports.md)：

| 端口 | 文件 | 契约 | 实现 |
| --- | --- | --- | --- |
| `LLMGateway` / `StreamResult` | [llm_gateway.py](../../app/domain/ports/llm_gateway.py) | 流式 / 非流式 / 结构化 LLM 调用 + 成本估算 + Token 计量 | [LLMService](../integration_doc/llm_doc/llm.md) |
| `ToolGateway` / `ToolResult` | [tool_gateway.py](../../app/domain/ports/tool_gateway.py) | 工具 Schema 导出 + 执行 | [ToolService](../integration_doc/tools_doc/tools.md) |
| `ContextBudgetPort` | [context_budget.py](../../app/domain/ports/context_budget.py) | 上下文预算（轮次 + token 双层护栏） | [ContextManager](../application_doc/context_doc/context.md) |
| `CostLimiterPort` | [cost_limiter.py](../../app/domain/ports/cost_limiter.py) | 成本上限（美元） | [CostLimiter](../application_doc/context_doc/context.md) |
| `EmbeddingPort` | [embedding_port.py](../../app/domain/ports/embedding_port.py) | 文本向量化（单条 / 批量） | [EmbeddingService](../integration_doc/embedding_doc/embedding.md) |

---

## 典型调用链路

```text
用户 → FastAPI 路由 → task_service / chat 路由（应用层）
    → BaseAgent.run()（领域层：生命周期 / 状态 / 事件路由）
        → _strategy_cycle()（策略编排，ReActAgent 委托 ReActStrategy）
            → ReActStrategy.execute()（推理 ↔ 工具循环）
                → LLMGateway.async_generate（集成层 LLM 调用，端口）
                → ToolGateway.execute（集成层工具执行，端口）
    → AgentResult → SSE 事件流回前端
```

领域层是这条链的**决策中枢**——集成层决定「模型怎么调、工具怎么跑」，领域层决定「什么时候推理、调什么工具、何时结束」。

---

## 配置关联

- 记忆配置（`memory_enabled` / `memory_max_short_term` / `memory_vector_db` / `memory_collection`）见 [记忆系统](memory_doc/memory.md)
- Agent 参数（`agent_max_iterations` / `llm_temperature` / `llm_max_tokens`）见 [Agent 模块](agent_doc/agent.md)
- 全部配置项见 [config 文档](../config_doc/config.md)

---

## 相关文档

- [架构设计](../architecture.md)
- [Agent 模块对外接口文档](agent_doc/agent.md) · [ReActAgent 桥接组件](agent_doc/executor.md)
- [提示词模块](prompts_doc/prompts.md)
- [记忆系统（预留）](memory_doc/memory.md)
- [推理策略](reasoning_doc/reasoning.md) · [ReActStrategy 策略组件](reasoning_doc/react.md)
- [领域端口契约](ports_doc/ports.md)
- [事件系统（共享层）](../shared_doc/events.md)
- [应用层说明](../application_doc/README.md)
- [集成层说明](../integration_doc/README.md)（端口实现方）
- [config 模块](../config_doc/config.md)
