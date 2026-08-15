# TaskService 说明文档（任务调度枢纽）

> **模块**：`app/application/task/task_service.py`
> **职责**：Agent 任务调度枢纽 —— 队列、优先级、并发、状态追踪、多 Agent 编排
> **文档定位**：顶层设计 + 对外说明；子模块详细设计见「子模块文档」规划（未来创建）

---

## 📋 目录

- [模块概述](#模块概述)
- [架构设计](#架构设计)
- [核心概念](#核心概念)
- [数据模型](#数据模型)
- [调度流程](#调度流程)
- [多 Agent 编排](#多-agent-编排)
- [子模块文档规划](#子模块文档规划)
- [配置项清单](#配置项清单)
- [当前状态与下一步](#当前状态与下一步)
- [相关文档](#相关文档)

---

## 模块概述

### 核心功能

TaskService 是系统的**任务调度枢纽**，负责编排 Agent 任务的完整生命周期：

- **任务队列**：任务按优先级入队，有界容量防无界堆积
- **并发控制**：限制同时运行的 Agent 任务数（防打爆 GPU/服务器）
- **状态追踪**：任务生命周期状态机（pending → running → completed/failed/cancelled）
- **多 Agent 编排**：主 Agent 拆分任务 → 并行调度子 Agent → 汇总结果

### 与 Agent 层的关系

```
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

### 当前实现状态

```python
# 当前 TaskService（仅并发闸门）
class TaskService:
    def __init__(self, max_concurrent=None):
        self._semaphore = asyncio.Semaphore(
            max_concurrent or settings.agent_max_concurrent_tasks
        )
    async def run_agent(self, user_input, messages, context, agent):
        async with self._semaphore:      # 并发限制
            async for event in agent.run(...):
                yield event
```

> **当前只做并发控制**；队列、优先级、状态、多 Agent 编排为规划方向（见「当前状态与下一步」）。

---

## 架构设计

### 设计理念

1. **调度与执行解耦**
   - 调度层（TaskService）决定「哪个任务何时执行」
   - 执行层（Agent）决定「单个任务如何执行」
   - 二者通过「任务」抽象衔接，互不感知内部实现

2. **优先级调度**
   - 任务带优先级（urgent / high / normal / low），高优先级先执行
   - 有界队列：任务超限时背压（拒绝或等待），防无界堆积

3. **worker 池模式**
   - 固定数量 worker 协程从队列取任务执行
   - 并发度 = min(队列容量, worker 数, 并发信号量)

4. **编排可扩展**
   - 主从并行：主 Agent 拆分 → 子 Agent 并行 → 汇总
   - 预留流水线链式 / 动态图编排（后续）

### 分层结构

```
TaskService（调度枢纽）
    │
    ├── TaskQueue        优先级队列
    │      多级队列（urgent/high/normal/low），有界容量，背压
    │
    ├── WorkerPool       worker 协程池
    │      agent_worker_pool_size 个 worker，从队列取任务执行
    │
    ├── TaskState        任务状态机
    │      pending / running / completed / failed / cancelled
    │
    └── Orchestrator     多 Agent 编排
          主 Agent 拆分 → 子 Agent 并行 → 汇总
```

---

## 核心概念

### 任务（Task）

一个可调度的 Agent 任务单元。对应一次 `Agent.run()` 执行（可能包含多轮 ReAct 循环）。

### 子任务（SubTask）

主 Agent 拆分的子任务，交给子 Agent 独立执行。子任务间可能有依赖（`depends_on`）。

### 优先级（Priority）

任务的调度优先级，决定在队列中的出队顺序：

| 优先级 | 默认超时 | 使用场景 |
| --- | --- | --- |
| `urgent` | 600s（高优先级超时） | 实时对话、紧急任务 |
| `high` | 600s | 重要但非紧急的分析 |
| `normal` | 300s（默认） | 常规任务 |
| `low` | 180s（低优先级超时） | 后台批处理 |

### 并发（Concurrency）

同时运行的 Agent 任务数上限（`agent_max_concurrent_tasks`）。超出部分在队列中等待。

### 编排（Orchestration）

多 Agent 协同完成任务的方式。当前规划主从并行模式。

---

## 数据模型

### `Task` — 任务

```python
@dataclass
class Task:
    task_id: str                    # 任务唯一标识
    user_request: str               # 用户原始请求
    priority: str = "normal"        # urgent/high/normal/low
    status: TaskState = TaskState.PENDING  # 当前状态
    parent_task_id: str | None = None      # 父任务（子 Agent 任务）
    sub_tasks: list[SubTask] = field(default_factory=list)  # 主任务拆分的子任务
    agent_result: AgentResult | None = None   # 执行结果
    error: str | None = None        # 失败原因
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
```

### `SubTask` — 子任务

```python
@dataclass
class SubTask:
    subtask_id: str                 # 子任务唯一标识
    description: str                # 子任务描述（发给子 Agent）
    priority: str = "normal"
    depends_on: list[str] = field(default_factory=list)  # 依赖的子任务
    result: AgentResult | None = None
    status: TaskState = TaskState.PENDING
```

### `TaskState` — 状态枚举

```python
class TaskState(Enum):
    PENDING = "pending"       # 排队中
    RUNNING = "running"       # 执行中
    COMPLETED = "completed"   # 成功完成
    FAILED = "failed"         # 失败
    CANCELLED = "cancelled"   # 被取消
```

**状态转换：**

```
PENDING → RUNNING → COMPLETED
                 ↘ FAILED
                 ↘ CANCELLED（用户取消）
```

---

## 调度流程

### 完整流程

```
submit_task(user_request, priority="normal")
    │
    ├─ 1. 创建 Task，置 PENDING
    ├─ 2. 入 TaskQueue（按优先级排序，有界容量）
    │
    ├─ 3. Worker 从队列取出（PENDING → RUNNING）
    │     └─ 受并发信号量约束（超出则等待）
    │
    ├─ 4. 主 Agent 执行（Agent.run()）
    │     └─ 若是批量任务 → Orchestrator 编排
    │
    ├─ 5. 完成（RUNNING → COMPLETED / FAILED）
    │
    └─ 6. 返回 TaskResult（含 agent_result、子任务结果）
```

### 交互式 vs 批量

| 模式 | 触发 | TaskService 行为 |
| --- | --- | --- |
| 交互式（chat 路由） | HTTP 请求直接驱动 | 当前：仅并发闸门，流式返回 |
| 批量（未来 agent 路由） | `submit_task` 异步提交 | 完整调度：队列 + 优先级 + 状态查询 |

---

## 多 Agent 编排

### 主从 + 并行子 Agent（规划）

```
用户请求
    │
    ▼
主 Agent（planning prompt 拆分任务）
    │  plan_generated 事件
    ▼
SubTask[1..n]（并行调度）
    ├── 子Agent 1 ──→ result1
    ├── 子Agent 2 ──→ result2   （受并发信号量约束）
    └── 子Agent 3 ──→ result3
    │
    ▼
主 Agent 汇总 → 最终答案
```

### 关键设计点

1. **子 Agent 共享 LLM/Tools**：默认复用全局 `LLMService` / `ToolService`（Explore 确认可行）
2. **模型切换**：子 Agent 可通过 `AgentContext.metadata["model_key"]` 指定不同模型（main/fast/reasoning）
3. **依赖**：`SubTask.depends_on` 支持链式（有依赖的子任务等待前置完成）
4. **事件**：新增 `task_submitted` / `task_started` / `task_completed` / `agent_spawned` / `plan_generated` / `aggregation_complete` 事件（复用 `build_sse_event`）

### 预留模式

| 模式 | 说明 | 状态 |
| --- | --- | --- |
| 主从 + 并行 | 主拆分 → 子并行 → 汇总 | 本次规划 |
| 流水线链式 | 子任务按依赖链顺序执行 | 预留 |
| 动态图编排 | 主 Agent 动态决定子任务协作方式 | 预留 |

---

## 子模块文档规划

> 类似 `docs/llm/` 文档体系，各子模块独立成文。未来实现时逐模块补全。

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
| `agent_max_concurrent_tools` | 3 | 单任务最大并发工具数 | ✅ 已用（ToolService） |
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
- **工具级并发**：`ToolService` 信号量 + `ReActAgent._execute_tool_calls` 并行（Agent 维度）

### 规划中（本次仅顶层设计）

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

- [Agent 模块说明](../../domain_doc/agent_doc/agent.md)（Agent 层：单任务执行）
- [配置管理模块](../../config_doc/config.md)（任务/并发配置）
- [工具模块](../../integration_doc/tools_doc/tools.md)（工具级并发）
