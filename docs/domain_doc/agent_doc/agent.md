# Agent 模块对外接口文档

> **对应代码**：`app/domain/agent/`
> **更新日期**：2026-08-29
> **文档定位**：Agent 模块对外接口文档——`BaseAgent` 统一入口的接口契约 + 内部组件导航；
> 服务对象为 Agent 模块的**外部调用方**（应用层 / API 层）
> **实现状态**：✅ 已实现（BaseAgent + ReActAgent；PlannerAgent / ReflectionAgent 预留）
> **配套**：实现依赖领域端口 `LLMGateway` / `ToolGateway`（可选 `ContextBudgetPort`）；推理策略实现见
> [reasoning 模块](../reasoning_doc/reasoning.md)（ReAct 策略在 `reasoning/react.py`）

---

## 📋 目录

- [Agent 模块对外接口文档](#agent-模块对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [对外接口](#对外接口)
    - [数据契约](#数据契约)
    - [BaseAgent 方法表](#baseagent-方法表)
    - [ReActAgent](#reactagent)
    - [对外异常契约](#对外异常契约)
    - [最小调用示例](#最小调用示例)
  - [内部实现组织](#内部实现组织)
  - [SSE 事件流](#sse-事件流)
  - [配置关联](#配置关联)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

Agent 模块是系统的**决策与行动核心**，负责编排 LLM 推理与工具调用的循环流程：

- **统一入口**：`BaseAgent.run()` 流式产出 SSE 事件，屏蔽策略差异——外部调用方以统一方式驱动任何 Agent
- **策略模式**：`BaseAgent` 定义统一入口与生命周期，具体推理策略由子类 `_strategy_cycle()` 实现
- **无状态设计**：每次 `run()` 新建实例，上下文经 `AgentContext` 传入
- **事件流驱动**：推理过程 / 工具调用 / 结果实时推送为 SSE 事件

### 模块结构

```text
app/domain/agent/
├── __init__.py          # 模块导出（AgentState / AgentContext / AgentResult / BaseAgent / ReActAgent）
├── base.py              # 基类与数据定义（AgentState, AgentContext, AgentResult, BaseAgent）
├── executor.py          # ReActAgent（桥接 reasoning/react.py 的 ReActStrategy）
├── planner.py           # PlannerAgent（Plan-then-Execute，预留）
└── reasoning.py         # ReflectionAgent（预留）
```

### 设计原则

1. **策略模式**：`BaseAgent.run()` 统一入口管理异常 / 状态 / 事件；`_strategy_cycle()` 抽象策略接口，子类选择并组合具体推理策略
2. **编排与实现分离**：agent/ 层管策略编排与生命周期，reasoning/ 层管策略实现（原子推理算法）——依赖方向 `agent → reasoning`，策略层不反向依赖
3. **无状态**：Agent 实例每次 run 新建，运行期间上下文不变
4. **LLM / Agent 分层清晰**：LLM 层单轮推理、Token 提取、连接重试；Agent 层循环编排、工具调用、结果判定

### 依赖关系

```text
外部调用方（chat.py 路由 / task_service / 应用层）
        ▼ 构造 + run()
ReActAgent（executor.py）── BaseAgent（base.py）── 依赖倒置
        │                                    ├── LLMGateway 端口（app/domain/ports/llm_gateway.py）
        │                                    └── ToolGateway 端口（app/domain/ports/tool_gateway.py）
        └── ReActStrategy（reasoning/react.py）── ReAct 主循环算法
```

---

## 对外接口

> 对外接口 = 被外部文件（应用层 / API 层）依赖的接口。`BaseAgent` 是模块对外的统一依赖面；
> 内部组件接口（`ReActStrategy` 等）见「内部实现组织」。

### 数据契约

#### `AgentState`（状态枚举）

```text
IDLE → THINKING →（工具调用）→ WAITING → THINKING → ... → COMPLETED / FAILED
                                                      ↘ CANCELLED（用户取消）
```

#### `AgentContext`（上下文，不可变值对象）

| 字段 | 类型 / 默认 | 说明 |
| --- | --- | --- |
| `session_id` / `user_id` | `SessionId` / `UserId`（必填） | 会话 / 用户标识 |
| `max_iterations` | `int = 10` | 最大推理轮数 |
| `temperature` | `float = 0.2` | 采样温度 |
| `max_tokens` | `int = 4096` | 单轮最大输出 token |
| `max_execution_time` | `float \| None = None` | 整个 ReAct 循环总时长上限（秒）；None=不设限（生产值由装配根注入 `agent_timeout`） |
| `max_context_rounds` | `int \| None = None` | 上下文预算：保留最近 N 轮 assistant/tool 配对；None=不裁剪（生产值 `agent_max_context_rounds`） |
| `max_context_tokens` | `int \| None = None` | 上下文预算：消息总 token 上限；None=不裁剪 |
| `max_empty_retries` | `int = 2` | 连续空输出重试上限（0=首次空输出即终止，生产值 `agent_max_empty_retries`） |
| `metadata` | `dict = {}` | 扩展字段（如 `model_key`） |

传递原则：值对象，每次 `run()` 传入，运行期间不变。`iteration_limit` 属性为 `max_iterations` 的语义别名。

#### `AgentResult`（执行结果，经 `agent.result` 读取）

| 字段 | 说明 |
| --- | --- |
| `success` | 是否成功 |
| `content` / `reasoning` | 最终回答 / 完整推理过程（累计） |
| `structured` | 结构化最终答案（final_answer 工具产出，`output_schema` 启用时） |
| `tool_calls` | 工具调用记录（`{tool, params, result, success, error, error_code, duration}` 列表） |
| `iterations` | 实际执行轮数 |
| `total_tokens` / `usage` | Token 总数 / 明细（prompt/completion/total，累计） |
| `error` | 失败原因 |
| `metadata` | 扩展字段 |

### BaseAgent 方法表

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `run(user_input, messages, context)` | 异步生成器 | 统一入口：管理状态 / 异常 / 事件路由；yield SSE 事件字符串；完成后经 `result` 读取 |
| `state` | 属性 | 当前状态（`AgentState`） |
| `result` | 属性 | 最终结果（`run()` 完成后调用；失败为 `success=False` + `error`） |
| `on_thought` / `on_tool_call` / `on_tool_result` / `on_complete` | 异步钩子 | 子类可覆盖的扩展点 |

`run()` 职责：上下文保存 → 状态重置 → `_strategy_cycle()` 事件转发 → 按 `_result.success` 置 COMPLETED/FAILED；取消（`CancelledError`）与未捕获异常经 `ErrorHandlerRegistry` 分发（默认转 CANCELLED / FAILED 并产对应事件，见「对外异常契约」）。

### ReActAgent

当前唯一实现的 Agent 类型，ReAct 策略的编排载体：

```python
agent = ReActAgent(llm=llm_service, tools=tool_service)
# 可选横切能力注入：context_budget=context_manager, error_handlers=error_handler_registry
```

构造：`ReActAgent(llm, tools, context_budget=None, error_handlers=None)`——`context_budget` 为上下文预算端口（应用层 ContextManager 注入，见 [ports.md](../ports_doc/ports.md)），`error_handlers` 为错误处理注册表（共享内核横切入口，见「对外异常契约」）。`BaseAgent(llm, tools, error_handlers=None)` 同构。

- `_strategy_cycle` 委托 `ReActStrategy.execute()`（ReAct 主循环），产出事件 + 组装 `AgentResult`
- 行为契约（ReAct 循环）：推理 → finish_reason 分支 → 工具调用 / 正常结束 / 空输出重试 → 迭代兜底
- 实现细节见 [executor.md](executor.md)（编排）与 [react.md](../reasoning_doc/react.md)（算法）

### 对外异常契约

Agent 模块错误处理经共享内核 `ErrorHandlerRegistry` 横切分发（注入 BaseAgent / ReActStrategy，见 [error_handling 文档](../../shared_doc/error_handling.md)）。9 类 `AgentErrorKind` 按策略分发（`CONTINUE` / `STOP` / `RAISE`），调用方可按 kind 注册覆盖；未注入时使用默认行为：

| `AgentErrorKind` | 默认 action | 场景 → 默认处理 |
| --- | --- | --- |
| `LLM_FAILED` | STOP | LLM 调用失败 → 循环短路，返回失败结果（不空转重试） |
| `EMPTY_OUTPUT` | CONTINUE | LLM 未生成有效输出 → 重试下一轮 |
| `MAX_TURNS` | STOP | 迭代耗尽 → 用最后结果兜底结束 |
| `TIMEOUT` | STOP | 总时长超限 → 降级（error 记录超时） |
| `CANCELLED` | STOP | 外部取消 → `state=CANCELLED` |
| `UNKNOWN` | STOP | 未捕获异常 → `state=FAILED`，产 error 事件 |
| `TOOL_FAILED` | CONTINUE | 工具执行失败 → 回喂模型自纠 |
| `PARSE_FAILED` | CONTINUE | 工具参数 JSON 解析失败 → 回喂自纠 |
| `STRUCTURED_INVALID` | CONTINUE | final_answer 参数校验失败 → 回喂自纠 |

调用方视角：默认行为下 `run()` 不抛异常（取消 / 失败均收敛为对应状态 + 结果）；仅当调用方注册 handler 决策 `RAISE` 时，上抛 `AgentRunError`（定义于 `app.shared.error_handling`）——这是 Agent 模块被外部捕获的唯一领域异常类型。

### 最小调用示例

```python
from app.domain.agent import AgentContext, ReActAgent

agent = ReActAgent(llm=llm_service, tools=tool_service)
ctx = AgentContext(session_id="sess_001", user_id="user_001")
messages = [{"role": "system", "content": "你是一个智能助手"},
            {"role": "user", "content": "查询今天的天气"}]

async for event in agent.run("查询今天的天气", messages, ctx):
    yield event  # 转发 SSE 事件

result = agent.result
print(result.content, result.tool_calls, result.usage)
```

---

## 内部实现组织

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| [executor.md](executor.md) | `executor.py` | ReActAgent：桥接 ReActStrategy 到 BaseAgent 生命周期 | ✅ |
| planner.py | `planner.py` | PlannerAgent：Plan-then-Execute 编排（规划→执行→汇总） | ⬜ 预留 |
| reasoning.py | `reasoning.py` | ReflectionAgent：Reflection 编排（生成→自查→修正） | ⬜ 预留 |

**配套策略库**（[reasoning 模块](../reasoning_doc/reasoning.md)）：

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| [react.md](../reasoning_doc/react.md) | `reasoning/react.py` | ReActStrategy：推理 ↔ 工具循环原子算法 | ✅ |
| reflection | `reasoning/reflection.py` | Reflection 策略 | ⬜ 预留 |
| chain_of_thought | `reasoning/chain_of_thought.py` | CoT 策略 | ⬜ 预留 |

---

## SSE 事件流

Agent 模块对外产出的事件类型（与 LLM 层共用 `app.shared.events`）：

| 事件类型 | 产出者 | 触发时机 | 关键字段 |
| --- | --- | --- | --- |
| `reasoning` | LLM 层 | 模型输出思考 token | `content`（单 token） |
| `message` | LLM 层 | 模型输出回答 token | `content`（单 token） |
| `error` | LLM 层 / Agent | LLM 调用失败 / Agent 异常 | `content`（错误描述） |
| `tool_call` | Agent | LLM 决定调用工具 | `content`（工具名）、`params`、`iteration` |
| `tool_result` | Agent | 工具执行完成 | `content`（结果摘要）、`tool`、`duration`、`iteration` |
| `done` | Agent | Agent 结束（正常 / 强制） | `iterations`、`total_tokens` |
| `info` | Agent | 状态信息（开始 / 重试 / 超限） | `content`（描述） |

---

## 配置关联

Agent 模块与 `settings.py` 配置项关联（完整表见 [config 文档](../../config_doc/config.md)）：

| 配置项 | 默认值 | 影响范围 |
| --- | --- | --- |
| `agent_max_iterations` | 10 | `AgentContext.max_iterations` 默认值 |
| `llm_temperature` | 0.2 | `AgentContext.temperature` 默认值 |
| `llm_max_tokens` | 4096 | `AgentContext.max_tokens` 默认值 |
| `agent_timeout` | 300 | `AgentContext.max_execution_time` 生产值（循环总时长上限，秒） |
| `agent_max_context_rounds` | 8 | `AgentContext.max_context_rounds` 生产值（上下文预算保留轮数） |
| `agent_max_empty_retries` | 2 | `AgentContext.max_empty_retries` 生产值（连续空输出重试上限） |
| `agent_max_concurrent_tools` | 3 | 单任务工具级并发（ToolGateway） |

---

## 相关文档

- [领域层说明](../README.md)
- [ReActAgent 桥接组件](executor.md)
- [推理策略模块](../reasoning_doc/reasoning.md)（含 [react.md](../reasoning_doc/react.md)）
- [领域端口契约](../ports_doc/ports.md)
- [架构设计](../../architecture.md)
- [配置管理模块](../../config_doc/config.md)
- [工具模块说明](../../integration_doc/tools_doc/tools.md)
- [ADR agent-error-handling](../../../adr/domain/agent/2026-08-28-agent-error-handling.md)（错误处理横切入口）
