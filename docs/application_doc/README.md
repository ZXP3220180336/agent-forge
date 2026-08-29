# 应用层说明文档

> **对应代码**：`app/application/`
> **更新日期**：2026-08-29
> **文档定位**：应用层（`app/application/`）—— 会话、上下文、任务调度三个服务，是 API 层与核心层（Agent）之间的用例调度层。
> **实现状态**：Session（✅ 已实现）· Context（✅ 已实现）· Task（✅ 并发闸门已实现，队列/编排规划中）

---

## 📋 目录

- [应用层说明文档](#应用层说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [实现状态总览](#实现状态总览)
  - [典型调用链路](#典型调用链路)
  - [SessionManager 会话管理](#sessionmanager-会话管理)
  - [ContextManager 上下文管理](#contextmanager-上下文管理)
  - [TaskService 任务调度](#taskservice-任务调度)
  - [配置关联](#配置关联)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

应用层是系统的**用例调度层**，位于 API 层与核心层（Agent）之间，为 chat 主链路串起「会话 → 上下文 → 任务」：

- **会话管理**（`SessionManager`）：会话 CRUD + Redis 热缓存 + DB 持久化 + 分页/搜索/统计
- **上下文管理**（`ContextManager`）：组装 LLM messages + token 计数/超限截断 + Agent 运行中上下文预算管理（`ContextBudgetPort` 横切）
- **任务调度**（`TaskService`）：任务级并发信号量 + `run_agent()` 流式包装

### 模块结构

```text
app/application/
├── session/session_manager.py  ← SessionManager 会话管理
├── context/context_manager.py  ← ContextManager 上下文管理
└── task/task_service.py        ← TaskService 任务调度
```

下游能力（LLM / Tools / Embedding / Agent）在集成层与领域层，见 [集成层说明](../integration_doc/README.md) / [领域层说明](../domain_doc/README.md)。

### 设计原则

1. **单例装配**：`container` 持有全部服务实例，启动时经 `Container.initialize()` 统一初始化，关闭时统一清理
2. **用例编排**：三个服务对应 chat 主链路的三段职责——「拿到会话 → 组装请求 → 并发调度」
3. **调度与执行解耦**：`TaskService` 决定「任务何时并发执行」，Agent 决定「单个任务如何执行」
4. **降级容错**：单个基础设施（Redis / DB）初始化失败不影响整体启动，只记录警告并降级

### 依赖关系

```text
API 层（chat / session 路由）
        │
        ▼
SessionManager ◄──► ContextManager
        │
        ▼
TaskService.run_agent()
        │
        ▼
ReActAgent（app/domain/，经 ContextBudgetPort 复用 ContextManager.trim_messages）
        │
        ▼
LLMService / ToolService（app/integration/）
```

应用层内部依赖：`ContextManager` 依赖 `SessionManager`（会话数据）；`TaskService` 相对独立。基础设施（Redis / DB）由 `container` 直接管理并注入。下游 LLM / Tools / Agent 分属集成层与领域层，见 [集成层说明](../integration_doc/README.md) / [领域层说明](../domain_doc/README.md)。

---

## 实现状态总览

| 子模块 | 文件 | 状态 | 核心内容 |
| --- | --- | --- | --- |
| Session | `session/session_manager.py` | ✅ | 会话生命周期 + Redis 热缓存 + DB 持久化 + 分页/搜索/统计 |
| Context | `context/context_manager.py` | ✅ | messages 组装 + token 计数/截断 + 运行中上下文预算（ContextBudgetPort） |
| Task | `task/task_service.py` | ✅ | 任务级并发信号量 + `run_agent()` 流式包装（队列/编排规划中） |

---

## 典型调用链路

```text
POST /api/chat/send
  → SessionManager（会话验证 + 存用户消息）
  → ContextManager.build_messages（构建 messages，token 计数/截断）
  → TaskService.run_agent()（任务级并发信号量）
      → ReActAgent._strategy_cycle()（ReAct 循环）
          → LLMService.async_generate()（集成层）
          → ToolService.execute()（集成层）
  → SSE 事件流 → SessionManager.add_message（存 assistant 消息）
```

应用层在链路中承担三类职责：**入口编排**（Session + Context 负责「拿到会话 → 组装请求」）、**并发控制**（Task 任务级信号量）、**上下文护栏**（Context 经 ContextBudgetPort 在 Agent 循环中裁剪）。

---

## SessionManager 会话管理

**代码**：`app/application/session/session_manager.py` · **文档**：[会话管理详解](session_doc/session.md)

负责会话与消息两条数据链路的完整生命周期，Redis 热缓存 + DB 持久化双存储：

| 能力 | 说明 |
| --- | --- |
| 生命周期 | 创建 / 查询 / 软删除 / 硬删除 |
| 持久化 | Redis 热缓存 + DB 双存储，cache-through 读路径 |
| 查询 | 分页 / 搜索 / 筛选 / 排序 / 统计聚合 |

## ContextManager 上下文管理

**代码**：`app/application/context/context_manager.py` · **文档**：[上下文管理详解](context_doc/context.md)

从会话历史组装 LLM messages，token 精确计数 + 超限截断，并承担 Agent 运行中的上下文预算管理：

| 能力 | 说明 |
| --- | --- |
| 输入侧组装 | `build_messages`：system + 历史 + user，超限截断 |
| 运行中护栏 | `trim_messages`：轮次 + token 双层护栏（`ContextBudgetPort` 横切能力） |
| 依赖 | `SessionManager`（会话数据）+ `TokenCounter` 端口（计数） |

## TaskService 任务调度

**代码**：`app/application/task/task_service.py` · **文档**：[任务调度说明](task_doc/task.md)

任务级并发调度，限制同时运行的 Agent 任务数，流式包装 Agent 执行：

| 能力 | 说明 |
| --- | --- |
| 并发闸门 | 信号量限制 Agent 任务并发数（`agent_max_concurrent_tasks`） |
| 流式包装 | `run_agent()` 在信号量保护下逐事件 yield |
| 规划 | 队列 / 状态机 / 多 Agent 编排（见 [task.md](task_doc/task.md)） |

---

## 配置关联

- 上下文配置（`max_context_tokens` / `max_output_tokens` / `max_history_rounds`）见 [context.md](context_doc/context.md)
- 会话配置（`redis_url` / `database_url` / `database_pool_size` 等）见 [session.md](session_doc/session.md)
- 任务配置（`agent_max_concurrent_tasks`）见 [task.md](task_doc/task.md)
- 全部配置项见 [config 文档](../config_doc/config.md)

---

## 相关文档

- [架构设计](../architecture.md)（分层与核心链路）
- [Session 模块](session_doc/session.md)（会话管理详解）
- [Context 模块](context_doc/context.md)（上下文管理详解）
- [Task 模块](task_doc/task.md)（任务调度说明与规划）
- [集成层说明](../integration_doc/README.md)（下游能力：LLM / Tools / Embedding）
- [领域层说明](../domain_doc/README.md)（ReActAgent 与端口，本层下游调用方）
- [API 层说明](../api_doc/README.md)（本层上游调用方）
- [配置说明](../config_doc/config.md)
- [部署](../deployment.md)
