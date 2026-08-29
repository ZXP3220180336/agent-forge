# Agent 错误处理策略可扩展（领域层横切）

> 日期：2026-08-28 ｜ 层级：domain（横切，agent + reasoning）
> 定位：记录「错误处理策略可扩展」的工业级调研与设计方向。对标增强项 #23（当前 ⚠️，未实现）。本决策先调研定稿，实现另行安排。

## Context

- **现状**：错误处理硬编码且分散——`BaseAgent.run` 用 catch-all（CancelledError → CANCELLED / Exception → FAILED）；ReAct 循环内联 8 个错误分支（LLM 失败短路 / 空输出重试 / 迭代兜底 / 超时降级 / 工具失败回喂 / 解析失败回喂 / final_answer 校验回喂 / 结构化异常）。无注册机制，各 Agent 模式（Planner / Reflection）无法复用或定制。
- **需求**：错误处理策略应属**领域层横切能力**，不限于 ReAct——所有 Agent 模式共享，可按错误类型扩展/覆盖。
- 现有基础：`app/shared/exceptions.py` 统一异常树（AppError + AppErrorCode，含截断/拒答/工具调用分类）；工具层 `ExecutionHooks`（成功路径通知）。

### 领域层 vs 集成层错误处理的关系（为什么不是一套系统）

本项目现有**多层错误处理**，各司其职、共享同一异常树：

| 层 | 处理什么错误 | 机制 | 处理动作 |
| --- | --- | --- | --- |
| 共享层 `exceptions.py` | 全项目异常词汇表 | `AppError` 树 + `AppErrorCode` | 统一异常契约 |
| 集成层（LLM/工具） | 外部交互错误：传输（429/5xx/超时/4xx）、模型拒答、工具执行失败 | `RetryHandler` + `ErrorCategory`（重试/熔断/fallback）；`ToolResult` + `ErrorCode`（NOT_REGISTERED/JSON_PARSE/VALIDATION/...） | **技术兜底**：重试、熔断、降级、错误码标注 |
| 领域层（Agent 编排） | 编排错误：空输出、超轮次、超时、工具失败后的决策 | 当前硬编码分支；规划 `ErrorHandlerRegistry` | **策略决策**：继续循环 / 终止 / 回喂模型 / 兜底输出 |

**为什么不用一套系统**（四个维度）：

1. **错误形态不同**：集成层 = 进程外故障（网络 / 限流 / 模型行为），技术性、可重试分类；领域层 = 进程内流程决策（语义性、策略性）
2. **处理动作不同**：集成层 = 让调用成功（重试瞬时故障 / 降级备用模型 / 熔断保护）；领域层 = 编排 Agent 行为（LLM 失败短路 vs 工具失败回喂自纠 vs 终止）
3. **抽象层级与依赖方向**：领域层不 import 集成层（依赖倒置），经**端口契约**感知错误（`StreamResult.error` / `ToolResult.error_code`）；集成层的重试 / 熔断对领域层透明（领域层只看到「调用最终失败」）
4. **工业界同样分离**：OpenAI 传输层自动重试 + Agent 层 `error_handlers`；LangGraph `RetryPolicy` + `error_handler` 解耦是官方推荐

**关系：转译衔接，而非替代**——集成层兜底失败后，经端口契约把语义错误交给领域层决策；两层共享 `exceptions.py` 异常词汇，形成「统一异常树 + 分层处理策略」。类比：TCP 重传（传输层，技术性可重试）vs 应用层业务超时（编排层，语义性策略决策）。

## 一、工业级调研（四要素 + 各框架）

### 共同模式

> **错误分类（怎么描述错误）→ 注册机制（怎么挂自定义处理）→ 分发时机（在哪触发）→ 决策动作（处理完能做什么）**

两条哲学路线：

- **回喂路线**（SMOLagents / LangGraph ToolNode / OpenAI 工具层 / LangChain `handle_parsing_errors`）：模型输出不可用但可自纠的错误（解析失败、工具参数错误、工具执行异常）→ 转消息**回喂模型**，模型换思路/换参数，循环继续。错误 = 下一次输入。
- **拦截路线**（OpenAI `error_handlers` / LangGraph `error_handler` / Claude hooks）：循环内不可自愈的**终结性错误**（超轮数、拒答、结构化校验失败、节点崩溃）→ 交外部 handler/hook 决策「兜底输出 / 重试 / 路由补偿 / 上抛」。错误 = 一次决策点。

### 各框架详细

| 框架 | 错误分类 | 注册机制 | 分发时机 | 决策动作 | 定位 |
| --- | --- | --- | --- | --- | --- |
| **OpenAI Agents SDK** | 异常层级 `AgentsException` 家族（MaxTurnsExceeded / ModelRefusalError / ModelBehaviorError / ToolTimeout 等）+ 种类键 `max_turns` / `model_refusal` / `invalid_final_output` | `error_handlers: RunErrorHandlers` dict（TypedDict，`RunErrorHandler = Callable[[HandlerInput], MaybeAwaitable[HandlerResult \| Any \| None]]`） | 循环**终结性错误点**（超轮数 / refusal / 结构化校验失败） | handler 返回**兜底 final_output**（SDK 校验后结束 run，`include_in_history` 控制持久化）；返回 None 或未注册 → **上抛原异常** | 结果导向（双层：可恢复→`tool_error_formatter` 回喂；终结→handler） |
| **LangGraph** | 异常家族 + `ErrorCode` 枚举（GRAPH_RECURSION_LIMIT 等） | 图节点属性 `retry_policy=RetryPolicy(...)` + `error_handler=...`（功能 API `@task(retry_policy=...)`）；`set_node_defaults` 图级默认 | 节点失败；**retry 耗尽后**才触发 error_handler（二者解耦） | RetryPolicy（重试瞬时错误，`default_retry_on` 保守：不重试 ValueError/TypeError/控制流信号）；error_handler 更新 State + `Command(goto=...)` **路由补偿**（Saga 模式）；checkpoint 恢复 | 编排导向 |
| **Claude Agent SDK** | 错误语义分布在各 hook 事件 + `ResultMessage.error_*` 子类型（**无 `on_error` / `Error` / `StopFailure` hook**） | `hooks: dict[HookEvent, list[HookMatcher]]`（按工具名/事件名匹配） | hook 事件点 | `PostToolUseFailure`（工具失败，非 blockable，可注入 `additionalContext` 引导模型）；hooks 可 `continue_: False` + stopReason 终止；**工具错误由 handler 返回 `isError` 自定义文本回喂**（SDK 不模板化，无 `tool_error_formatter`）；**无「返回兜底输出」的一等语义** | 事件观察 |
| **SMOLagents** | `AgentError` 家族：AgentParsingError / AgentExecutionError / AgentToolCallError / AgentMaxStepsError / AgentGenerationError（子类即语义） | 循环内 `except AgentGenerationError: raise` / `except AgentError: 记录+继续`；`dict()` 以类名入 memory（证据链） | 循环错误点 | 模型侧错误 → `ActionStep.error` 回喂 memory（模型可见自纠）；生成错误（实现 bug）→ 上抛；max steps → 兜底 final answer | 分类即策略（最内联，扩展靠覆写） |
| **LangChain** | `handle_parsing_errors: bool \| str \| Callable` | Runnable 组合子 `with_retry` / `with_fallbacks`；v1 middleware 体系 | Agent 解析错误点 / Runnable 链 | 解析错误回喂 observation；retry/fallback 降级 | 组合子式 |
| **AutoGen** | 无形式化分类 | 终止条件（is_termination_msg / TerminationCondition）+ max_retries 参数 | — | 弱（终止 + 重试参数，无集中错误处理扩展点） | 弱可扩展 |

### 差异取舍

- **OpenAI** 终结错误「结果导向」：handler 返回兜底输出、SDK 校验并收尾 run；**无原生自动重试**（防工具副作用重放）。
- **LangGraph** 最「编排导向」：retry 与 error_handler 解耦，支持补偿路由与 checkpoint 恢复；上手成本高。
- **Claude** 最「事件观察」：hooks 强可阻断，无终结兜底语义。
- **SMOL** 最「分类即策略」：异常子类即处理语义，简洁；扩展靠覆写。

## 二、本项目设计方向（基于工业级参照）

### 错误分类（双轨，对齐 OpenAI + SMOL）

- **`AgentErrorKind`（枚举）**：handler 分发键——`MAX_TURNS` / `LLM_FAILED` / `EMPTY_OUTPUT` / `TIMEOUT` / `TOOL_FAILED` / `PARSE_FAILED` / `STRUCTURED_INVALID` / `MODEL_REFUSAL` / `CANCELLED` / `UNKNOWN`
- **异常层级**（可后续）：`AgentError(Exception)` 携带证据链上下文，子类划分（复用现有 `AppError` 体系或独立）

### Handler 协议（对齐 OpenAI，异步可）

```python
AgentErrorHandler = Callable[
    [AgentErrorContext],
    MaybeAwaitable[AgentErrorAction],       # CONTINUE / STOP / RAISE
]
AgentErrorContext: kind + message + iteration + 相关数据（结果载体/证据链快照）
```

### 注册 / 分发机制（领域层横切）

- **`ErrorHandlerRegistry`**（`app/shared/error_handling.py`，共享内核）：`register(kind, handler)` / `dispatch(kind, ctx) -> AgentErrorAction`；**默认 action = 现有行为**（短路失败 / 空输出重试 / 迭代兜底 / 降级 / 可恢复回喂）
- **`BaseAgent` 构造注入 registry**——所有 Agent 模式（ReAct/Planner/Reflection）共享横切入口
- **ReAct 循环错误分支改为分发**：`action = await registry.dispatch(kind, ctx)`，按 action 处理；`BaseAgent.run` 的 catch-all 也归入 kind 分发

### 双层策略（对齐 OpenAI 双层模型）

- **可恢复错误**（解析失败 / 工具参数错误 / 工具执行异常）→ **回喂模型自纠**（已实现，保持）
- **终结性错误**（max_turns / 超时 / 结构化校验失败 / 拒答 / 取消）→ **handler 决策**（兜底输出 / 重试 / 上抛）

### 默认行为（不扩展时）

- 终结错误 fail-loud 上抛（对齐 OpenAI 默认）；**不自动重试**（防工具副作用重放）
- 错误统一进证据链（对齐 OpenAI `attach_generic_agent_error` / SMOL `AgentError.dict()` 入 memory）

## Decision

本项目采用：**`AgentErrorKind` 枚举 + `AgentErrorHandler` 协议 + `ErrorHandlerRegistry`（默认 action = 现有行为）+ `BaseAgent` 横切注入**——对齐 OpenAI `error_handlers`（结果导向 + 双层），错误分类复用现有异常体系。**实现落于 `app/shared/error_handling.py`（共享内核）**：`reasoning` 与 `agent` 均可依赖（不违反「reasoning 只依赖 ports + shared」）。9 个 kind 全接入（终结 6 + 可恢复 3），ReAct 循环 6 处错误分支改为分发，默认行为零变化；Planner/Reflection 复用同一机制。不引入 LangGraph 式 retry_policy/补偿路由（当前单 Agent 编排无此需求，记升级路径）。

## Consequences

- ✅ 错误处理成为领域层横切能力：注册机制 + 默认行为，所有 Agent 模式共享
- ✅ 现有 ReAct 行为零变化（默认 handler 保留）；调用方按 kind 注册自定义处理
- ✅ 可恢复/终结双层对齐工业界；错误进证据链
- ✅ 已实现（2026-08-28）：`ErrorHandlerRegistry` 落于共享内核，9 kind 全接入（终结 6 + 可恢复 3）；对标 #23 ✅（调研依据：OpenAI SDK `run_error_handlers.py` / LangGraph `RetryPolicy`+`error_handler` / SMOL `AgentError` / Claude SDK hooks（`PostToolUseFailure` + `isError` 自定义文本回喂，专项源码级核实）/ LangChain `handle_parsing_errors`）
- 📌 升级路径：LangGraph 式 retry/补偿路由（多 Agent 编排时）；`AgentError` 异常层级落地
