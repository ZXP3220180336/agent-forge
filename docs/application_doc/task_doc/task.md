# TaskService 任务调度说明文档

> **对应代码**：`app/application/task/task_service.py`
> **更新日期**：2026-08-29
> **职责**：任务级并发调度（当前实现）；队列 / 状态追踪 / 多 Agent 编排（规划蓝图）
> **实现状态**：🔶 进行中——并发闸门（✅ 已实现），队列 / 状态机 / 编排（⬜ 规划）

---

## 📋 目录

- [TaskService 任务调度说明文档](#taskservice-任务调度说明文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [定位与职责](#定位与职责)
    - [与 Agent 层的关系](#与-agent-层的关系)
  - [当前接口契约](#当前接口契约)
  - [规划蓝图](#规划蓝图)
    - [架构设计](#架构设计)
    - [核心概念](#核心概念)
    - [数据模型](#数据模型)
    - [调度流程](#调度流程)
    - [多 Agent 编排](#多-agent-编排)
  - [子模块文档规划](#子模块文档规划)
  - [配置项清单](#配置项清单)
  - [当前状态与下一步](#当前状态与下一步)
    - [已实现](#已实现)
    - [规划中](#规划中)
    - [下一步](#下一步)
  - [相关文档](#相关文档)

---

## 模块概述

### 定位与职责

TaskService 是系统的**任务调度枢纽**，负责编排 Agent 任务的完整生命周期。当前实现只覆盖**并发闸门**；队列、优先级、状态追踪、多 Agent 编排为规划方向（见「规划蓝图」）。

### 与 Agent 层的关系

```text
API 层（chat / agent 路由）
    ↓
TaskService（任务调度枢纽）          ← 本模块
    ├── 并发闸门（Semaphore）        ← 已实现
    ├── 任务队列（优先级）           ← 规划
    ├── 状态追踪                     ← 规划
    └── Orchestrator（主从编排）     ← 规划
    ↓
Agent 层（BaseAgent / ReActAgent / 子Agent）
```

- **Agent 层**负责「单个任务的推理与工具循环」
- **TaskService** 负责「多个任务的调度与协同」——二者解耦，Agent 层无感知

---

## 当前接口契约

| 成员 | 同步/异步 | 说明 |
| --- | --- | --- |
| `__init__(max_concurrent=10)` | 构造 | 任务级并发信号量（装配根注入，对应 `agent_max_concurrent_tasks`） |
| `run_agent(user_input, messages, context, agent) -> AsyncGenerator[str]` | 异步生成器 | 信号量保护下运行 Agent，逐事件 yield（并发超限在此等待） |
| `max_concurrent` | property | 当前最大并发任务数 |

**关键语义**（示意，完整实现见源码）：

- 信号量在 `run_agent` 的 generator **外** acquire/release——yield 会挂起 generator frame，若 acquire 放 generator 内，其他任务会在首个 yield 前交错进入，信号量失去约束
- `async with` 天然保证异常 / 取消时释放信号量，不会挂死占坑
- 信号量是 **Agent 维度**（限制同时运行的 Agent 任务），而非 LLM API 维度（RPM / TPM 由集成层 `reservation_limiter` 覆盖，见 [LLM 层文档](../../integration_doc/llm_doc/llm.md)）

**最小调用示例**：

```python
# chat 路由经 TaskService 在任务级并发信号量保护下运行 Agent（见 app/api/routes/chat.py）
async for event in task_service.run_agent(
    user_input=request.message,
    messages=messages,
    context=ctx,
    agent=agent,
):
    yield event  # SSE 事件字符串
```

---

## 规划蓝图

> ⬜ 以下为多 Agent 任务编排的目标设计，对应架构文档演进路径。当前 `task_service.py` 仅实现并发闸门，未包含这些类型与流程。

### 架构设计

**设计理念**：

1. **调度与执行解耦**：调度层（TaskService）决定「哪个任务何时执行」，执行层（Agent）决定「单个任务如何执行」
2. **优先级调度**：任务带优先级（urgent / high / normal / low），有界队列超限时背压
3. **worker 池模式**：固定数量 worker 协程从队列取任务执行，并发度 = min(队列容量, worker 数, 信号量)
4. **编排可扩展**：主从并行 → 预留流水线链式 / 动态图编排

**分层结构**：

```text
TaskService（调度枢纽）
    │
    ├── TaskQueue        优先级队列（多级队列、有界容量、背压）
    ├── WorkerPool       worker 协程池（agent_worker_pool_size）
    ├── TaskState        任务状态机（pending/running/completed/failed/cancelled）
    └── Orchestrator     多 Agent 编排（主 Agent 拆分 → 子 Agent 并行 → 汇总）
```

### 核心概念

- **任务（Task）**：一个可调度的 Agent 任务单元，对应一次 `Agent.run()` 执行（可能包含多轮 ReAct 循环）
- **子任务（SubTask）**：主 Agent 拆分的子任务，交给子 Agent 独立执行，子任务间可有依赖（`depends_on`）
- **优先级（Priority）**：urgent（600s）/ high（600s）/ normal（300s 默认）/ low（180s），决定队列出队顺序
- **并发（Concurrency）**：同时运行的任务数上限 `agent_max_concurrent_tasks`，超出在队列中等待
- **编排（Orchestration）**：多 Agent 协同方式，当前规划主从并行模式

### 数据模型

⬜ 规划模型：`Task` / `SubTask` / `TaskState` 为目标数据结构，当前未定义。

```python
@dataclass
class Task:
    task_id: str                    # 任务唯一标识
    user_request: str               # 用户原始请求
    priority: str = "normal"        # urgent/high/normal/low
    status: TaskState = TaskState.PENDING
    parent_task_id: str | None = None      # 父任务（子 Agent 任务）
    sub_tasks: list[SubTask] = field(default_factory=list)
    agent_result: AgentResult | None = None
    error: str | None = None
    created_at / started_at / completed_at: float | None = None
```

```python
class TaskState(Enum):
    PENDING = "pending"     # 排队中
    RUNNING = "running"     # 执行中
    COMPLETED = "completed" # 成功完成
    FAILED = "failed"       # 失败
    CANCELLED = "cancelled" # 被取消
```

**状态转换**：`PENDING → RUNNING → COMPLETED / FAILED / CANCELLED`

### 调度流程

```text
submit_task(user_request, priority="normal")
  → 1. 创建 Task，置 PENDING
  → 2. 入 TaskQueue（按优先级排序，有界容量，背压）
  → 3. Worker 取任务（PENDING → RUNNING，受并发信号量约束）
  → 4. 主 Agent 执行（批量任务 → Orchestrator 编排）
  → 5. 完成（RUNNING → COMPLETED / FAILED）
  → 6. 返回结果（含 agent_result、子任务结果）
```

| 模式 | 触发 | TaskService 行为 |
| --- | --- | --- |
| 交互式（chat 路由） | HTTP 请求直接驱动 | 当前：仅并发闸门，流式返回 |
| 批量（未来 agent 路由） | `submit_task` 异步提交 | 完整调度：队列 + 优先级 + 状态查询 |

### 多 Agent 编排

**主从 + 并行子 Agent**：

```text
用户请求
  → 主 Agent（planning prompt 拆分任务）→ plan_generated 事件
  → SubTask[1..n] 并行调度（受并发信号量约束）→ 各子 Agent 独立执行
  → 主 Agent 汇总 → 最终答案
```

**关键设计点**：

1. **子 Agent 共享 LLM/Tools**：默认复用全局 `LLMService` / `ToolService`
2. **模型切换**：子 Agent 可指定不同模型（main / fast / reasoning）
3. **依赖**：`SubTask.depends_on` 支持链式（有依赖的子任务等待前置完成）
4. **事件**：新增 `task_submitted` / `task_started` / `task_completed` / `agent_spawned` / `plan_generated` / `aggregation_complete`（复用 `build_sse_event`）

**设计启示**（编排规划依据，来自多 Agent Orchestrator-Worker 实验，此处只留工业级结论）：

1. 子任务必须**自包含 + 依赖传递**——若产生 `depends_on`，需把前序子结果注入后续子 Agent，否则子 Agent 隔离拿不到会**冗余重算**
2. **「汇总」不是子任务**——拆出"依赖全部前置的汇总子任务"会让并行性归零（聚合是主 Agent 的职责，独立于子任务调度）
3. **提示词约束 + 程序校验双保险**——主 Agent 拆分不稳定，程序侧需依赖检测 + 合并护栏兜底，不能只靠提示词
4. **拆分质量决定并行价值**——拆不好（巨型子任务）并行性归零；拆分产物应是自包含的计算步骤

**预留模式**：

| 模式 | 说明 | 状态 |
| --- | --- | --- |
| 主从 + 并行 | 主拆分 → 子并行 → 汇总 | 本次规划 |
| 流水线链式 | 子任务按依赖链顺序执行 | 预留 |
| 动态图编排 | 主 Agent 动态决定子任务协作方式 | 预留 |

---

## 子模块文档规划

> 各子模块独立成文，未来实现时逐模块补全。

| 文档 | 内容 | 状态 |
| --- | --- | --- |
| [task.md](task.md) | 本文件：TaskService 总览 + 顶层计划 | ✅ 已创建 |
| `task/queue.md` | 优先级队列设计（多级队列、有界容量、背压） | 🔶 规划 |
| `task/state.md` | 任务状态机（转换、超时、取消） | 🔶 规划 |
| `task/worker.md` | worker 池设计（协程、并发、优雅关闭） | 🔶 规划 |
| `task/orchestrator.md` | 主从并行编排（拆分、并行调度、汇总） | 🔶 规划 |

---

## 配置项清单

TaskService 相关配置（`app/config/settings.py`）：

| 配置项 | 默认值 | 说明 | 使用 |
| --- | --- | --- | --- |
| `agent_max_concurrent_tasks` | 10 | 最大并发任务数 | ✅ 已用（信号量） |
| `agent_task_queue_size` | 50 | 任务队列大小 | 🔶 规划 |
| `agent_worker_pool_size` | 5 | worker 池大小 | 🔶 规划 |
| `agent_priority_levels` | [low/normal/high/urgent] | 优先级等级 | 🔶 规划 |
| `agent_default_priority` | normal | 默认优先级 | 🔶 规划 |
| `agent_high_priority_timeout` | 600 | 高优先级超时 | 🔶 规划 |
| `agent_low_priority_timeout` | 180 | 低优先级超时 | 🔶 规划 |
| `agent_priority_queue_size` | 100 | 优先级队列容量 | 🔶 规划 |

---

## 当前状态与下一步

### 已实现

- **并发闸门**：`agent_max_concurrent_tasks` 信号量，限制同时运行的 Agent 任务数
- **接入 chat 路由**：`task_service.run_agent()` 在任务级并发约束下运行 Agent

### 规划中

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| A | TaskQueue（优先级）+ TaskState + WorkerPool | 调度核心 |
| B | Orchestrator（主从并行子 Agent 编排） | 阶段 A |
| C | API 路由（agent.py 异步任务提交/查询）+ 编排事件 | 阶段 B |

### 下一步

1. **阶段 A**：实现 TaskQueue（多级优先级队列、有界容量、背压）+ TaskState 状态机 + WorkerPool
2. **阶段 B**：实现 Orchestrator——主 Agent 用 planning prompt 拆分 → 子 Agent 并行 → 汇总
3. **阶段 C**：agent.py 路由（`POST /api/tasks/submit` + `GET /api/tasks/{id}`）+ 编排事件
4. 每阶段配套子模块文档（`queue.md` / `state.md` / `worker.md` / `orchestrator.md`）

---

## 相关文档

- [应用层说明](../README.md)（TaskService 的定位）
- [Agent 模块说明](../../domain_doc/agent_doc/agent.md)（Agent 层：单任务执行）
- [集成层说明](../../integration_doc/README.md)（LLM / Tools 能力）
- [配置管理模块](../../config_doc/config.md)（任务/并发配置）
