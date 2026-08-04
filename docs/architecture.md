# 架构设计文档

> **更新日期**：2026-08-03
> **文档定位**：系统整体架构分层、核心链路与设计原则。模块级细节见各模块说明文档（见文末「相关文档」）。

---

## 📋 目录

- [架构设计文档](#架构设计文档)
  - [📋 目录](#-目录)
  - [架构分层](#架构分层)
    - [各层职责](#各层职责)
  - [核心调用链路](#核心调用链路)
    - [完整链路（HTTP → 回复）](#完整链路http--回复)
    - [Agent 引擎链路（零外部依赖）](#agent-引擎链路零外部依赖)
  - [设计原则](#设计原则)
  - [模块实现状态总览](#模块实现状态总览)
  - [相关文档](#相关文档)

---

## 架构分层

```
API 层（FastAPI 路由）
    ├── chat / session 路由（已实现）
    └── admin / agent / tool 路由（预留）
    ↓
服务层（app/services/）
    ├── LLMService（Facade）→ LLM 子包（8 模块）
    ├── SessionManager / ContextManager / ToolService / TaskService / EmbeddingService
    └── MemoryService（预留）
    ↓
核心层（app/core/）
    ├── Agent（BaseAgent + ReActAgent + 预留策略）
    ├── Prompts（提示词模板）
    ├── Memory / Reasoning（预留）
    └── Events（统一 SSE 事件）
    ↓
数据模型层（app/models/）
    ├── ORM（SessionModel / MessageModel）
    └── Schemas（预留）
    ↓
基础设施层（app/infrastructure/）
    ├── Database / RedisClient（由 app_state 直接管理）
    └── VectorStore / MessageQueue（预留）
    ↓
工具层（app/tools/）
    ├── BaseTool + 内置工具（search / readFile / writeFile / code_exec / web_browse）
    └── 外部工具（预留）
    ↓
配置层（app/config/）← 全部通过 settings 单例访问
```

### 各层职责

| 层 | 职责 | 关键文件 |
| --- | --- | --- |
| API 层 | 暴露 REST 接口，鉴权后驱动 Agent | `app/api/routes/` |
| 服务层 | 业务调度枢纽，串起会话/上下文/工具/任务 | `app/services/` |
| 核心层 | Agent 推理循环、提示词、事件 | `app/core/` |
| 数据模型层 | ORM 模型与 Pydantic Schema | `app/models/` |
| 基础设施层 | 数据库/Redis/向量/消息队列抽象 | `app/infrastructure/` |
| 工具层 | Agent 可执行能力集合 | `app/tools/` |
| 配置层 | 集中式配置，Pydantic 验证 | `app/config/settings.py` |

---

## 核心调用链路

### 完整链路（HTTP → 回复）

```
POST /api/chat/send
  → SessionManager（会话验证 + 存用户消息）
  → ContextManager（构建 messages，token 计数/截断）
  → TaskService.run_agent()（任务级并发信号量）
      → ReActAgent._strategy_cycle()（ReAct 循环）
          → LLMService.async_generate()（流式 + 重试/熔断/限流/整流）
          → ToolService.execute()（并行 + 工具级信号量）
  → SSE 事件流 → 存 assistant 消息
```

### Agent 引擎链路（零外部依赖）

```
构造 AgentContext + PromptManager 构建 system prompt
  → ReActAgent.run(user_input, messages, ctx)
      → LLM 流式 → reasoning/message token
      → finish_reason = tool_calls → ToolService.execute → tool_result
      → LLM 总结 → done 事件
  → agent.result（AgentResult）
```

---

## 设计原则

1. **策略模式**：`BaseAgent.run()` 统一入口，`_strategy_cycle()` 子类实现（ReAct 当前 / Plan-then-Execute、Reflection 预留）
2. **无状态 Agent**：每次 `run()` 新建实例，上下文经 `AgentContext` 传入
3. **LLM / Agent 分层**：LLM 层管单轮推理与可靠性，Agent 层管循环编排与工具
4. **统一事件流**：LLM 层与 Agent 层共用 `app/core/events.py` 的 SSE 事件定义
5. **服务统一入口**：工具系统经 `ToolService` 对外（容器+执行+统计+装配合并一处）
6. **调度与执行解耦**：TaskService 决定「哪个任务何时执行」，Agent 决定「单个任务如何执行」

---

## 模块实现状态总览

| 层 | 模块 | 状态 |
| --- | --- | --- |
| API | chat / session 路由 | ✅ 已实现 |
| API | admin / agent / tool 路由 | ❌ 预留 |
| API | middleware（auth/rate_limit/error_handler） | ❌ 预留 |
| 服务 | LLMService + LLM 子包（8 模块） | ✅ 已实现 |
| 服务 | SessionManager / ContextManager / ToolService / TaskService / EmbeddingService | ✅ 已实现 |
| 服务 | MemoryService | ❌ 预留 |
| 核心 | BaseAgent / ReActAgent / Events / Prompts | ✅ 已实现 |
| 核心 | planner / reasoning / Memory / Reasoning 子包 | ❌ 预留 |
| 数据模型 | SessionModel / MessageModel | ✅ 已实现 |
| 数据模型 | task / tool_log / schemas | ❌ 预留 |
| 基础设施 | database / redis / vector_store / message_queue | ❌ 预留（DB/Redis 由 app_state 直接管理） |
| 工具 | BaseTool + 5 个内置工具 | ✅ 已实现 |
| 工具 | external | ❌ 预留 |
| 配置 | settings.py | ✅ 已实现 |

---

## 相关文档

- [HANDOFF](HANDOFF.md)（项目交接，顶层计划/进度）
- [product](product.md)（产品方向）
- [agent 模块](core_doc/agent_doc/agent.md)
- [api 模块](api_doc/api.md)
- [config 模块](config.md)
- [logging 模块](logging.md)（全局日志框架）
- [LLM 层](service_doc/llm_doc/llm.md)
- [task 模块](service_doc/task_doc/task.md)
- [tool 模块](tool_doc/tools.md)
- [部署](deployment.md)
