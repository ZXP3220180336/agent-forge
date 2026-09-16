# ReActAgent 桥接组件说明

> **模块**：`app/domain/agent/executor.py`
> **更新日期**：2026-09-16
> **职责**：`ReActAgent`——把 ReAct 领域推理流程桥接到 `BaseAgent` 生命周期的具体 Agent 类型
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
> **配套**：算法实现见 [react.md](../reasoning_doc/react.md)（`ReActStrategy`）

---

## 📋 目录

- [ReActAgent 桥接组件说明](#reactagent-桥接组件说明)
  - [📋 目录](#-目录)
  - [定位与职责](#定位与职责)
  - [接口契约](#接口契约)
    - [`ReActAgent(BaseAgent)`](#reactagentbaseagent)
  - [行为边界](#行为边界)
  - [使用示例](#使用示例)
  - [设计决策](#设计决策)
  - [测试](#测试)
  - [相关文档](#相关文档)

---

## 定位与职责

`ReActAgent` 是 ReAct 流程的 **agent/ 桥接类型**——把 `ReActStrategy` 接入 `BaseAgent` 生命周期：

1. **可实例化的编排类型**：`BaseAgent` 是抽象类（`_strategy_cycle` 为抽象方法），`ReActAgent` 实现之，是应用层可构造、可运行的 ReAct Agent 类型
2. **生命周期接入**：继承 `BaseAgent.run()` 的状态机 / 异常处理 / SSE 事件路由 / 结果存储
3. **策略适配**：把 `AgentContext` 显式映射为运行身份、模型、执行限制、上下文、恢复预算和工具执行六类不可变值对象（策略不依赖 AgentContext）；把策略产出 `ReActOutcome` 组装为 `AgentResult`

> 本组件只做桥接，不承载 ReAct 算法；算法在 `reasoning/react.py`。

---

## 接口契约

### `ReActAgent(BaseAgent)`

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(llm: LLMGateway, tools: ToolGateway, context_budget: ContextBudgetPort \| None = None, error_handlers: ErrorHandlerRegistry \| None = None, cost_limiter: CostLimiterPort \| None = None, cancel_event: asyncio.Event \| None = None)` | 构造 `ReActStrategy(llm, tools, context_budget, error_handlers, cost_limiter)`；`context_budget` 为上下文预算端口（ContextManager 注入），`error_handlers` 为错误处理横切入口，`cost_limiter` 为成本护栏端口（CostLimiter 注入），`cancel_event` 为优雅取消信号（/chat/stop 经 TaskService 置位，None=不启用） |
| `_strategy_cycle` | `(user_input, messages) -> AsyncGenerator[str]` | 从 `AgentContext` 构造六类 reasoning 值对象后委托 `strategy.execute(...)`，并显式透传 `ctx.stream_mode`；结束后 `_map_outcome` 组装 `AgentResult` |
| `_map_outcome` | `(outcome: ReActOutcome \| None) -> AgentResult` | 策略产出 → AgentResult 契约转换 |

**对外 API（继承自 `BaseAgent`）**：`run(user_input, messages, context)`（流式 SSE）、`state`、`result`——契约见 [agent.md](agent.md)。

**依赖方向**：`agent → reasoning`（编排调用策略）；策略层不反向依赖 agent/。

---

## 行为边界

| 场景 | 处理 |
| --- | --- |
| `AgentContext` 未设置 | `_strategy_cycle` 抛 `RuntimeError` |
| run 身份透传 | 三种 Agent 桥接把 `run_id`、`run_stop`、workflow 与父取消信号原样传给策略；子 ReAct 不创建新 run |
| 实例并发复用 | BaseAgent 在第二个并发 `run()` 入口明确拒绝，避免 `_context/outcome` 互相覆盖 |
| 策略未产出结果（`outcome is None`） | `_map_outcome` 返回失败 `AgentResult`（`success=False` + `error`） |
| LLM 调用失败 | 经策略短路返回失败结果（LLM-001，见 react.md 行为边界） |
| `ctx.stream_mode=False`（非流式通道） | 经 `_strategy_cycle` 透传策略走非流式 `generate()` 通道（一次拿完整结果，reasoning/message 整条事件），面向后台子 Agent（Phase C）；默认 `True` 流式行为不变 |
| 用户取消（优雅） | `cancel_event` 置位 → 策略层主循环顶部 / LLM error 分支识别 → `CANCELLED` 分发（不重试，保留部分进度）→ `state=CANCELLED`（REASON-003） |
| 用户取消（硬） | `BaseAgent.run()` 捕获 `asyncio.CancelledError` → `state=CANCELLED`（独立路径，与优雅取消并存） |

---

## 使用示例

```python
from app.domain.agent import AgentContext, ReActAgent

agent = ReActAgent(llm=llm_service, tools=tool_service)
ctx = AgentContext(
    session_id="sess_001", user_id="user_001",
    run_id=run_id, run_stop=run_stop,
)
async for event in agent.run("查询良率", messages, ctx):
    yield event  # 转发 SSE 事件
result = agent.result  # AgentResult
```

---

## 设计决策

- **为什么桥接而非在 executor 内嵌流程**：遵循 agent/（生命周期与结果桥接）+ reasoning/（领域流程）边界，使 ReActStrategy 可由 PlannerStrategy / ReflectionStrategy 组合并独立测试
- **为什么不转发 `execute_tool_calls()`**：工具并行原语只有策略层真实调用方；Planner / Reflection 组合完整 `ReActStrategy.execute()`，Agent 桥接不暴露无生产消费者的重复入口
- **决策记录**：[ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)

---

## 测试

`tests/unit/test_agent.py`：

- `test_strategy_cycle_short_circuits_on_llm_error` — LLM 失败短路返回失败结果（LLM-001）
- `test_unknown_handler_raise_propagates` — 未注册 kind 的 handler 决策 RAISE 时上抛
- `test_agent_error_reraisd_not_swallowed` — AgentRunError 不被 run() 吞掉，上抛给调用方
- `test_react_agent_passes_cost_limiter_to_strategy` — cost_limiter 构造透传策略
- `test_react_agent_passes_max_empty_retries_to_strategy` — max_empty_retries 经 AgentContext 透传
- `test_react_agent_passes_max_llm_fail_retries_to_strategy` — max_llm_fail_retries 经 AgentContext 透传
- `test_react_agent_passes_max_tool_protocol_retries_to_strategy` — max_tool_protocol_retries 经 AgentContext 透传
- `test_react_agent_passes_max_same_action_turns_to_strategy` — max_same_action_turns 经 AgentContext 透传
- `test_react_agent_passes_stream_mode_to_strategy` — stream_mode 经 AgentContext 透传策略：哨兵假 LLM 仅实现 generate（无 async_generate），证明 `ctx.stream_mode=False` 走非流式通道

策略算法本身由 `tests/unit/test_react_strategy.py` 覆盖，见 [react.md](../reasoning_doc/react.md)。

---

## 相关文档

- [Agent 模块对外接口文档](agent.md)（主文档）
- [ReActStrategy 策略组件](../reasoning_doc/react.md)（同级组件）
- [推理策略模块](../reasoning_doc/reasoning.md)
- [领域层说明](../README.md)
- [ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)
