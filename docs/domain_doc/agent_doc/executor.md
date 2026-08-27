# ReActAgent 桥接组件说明

> **模块**：`app/domain/agent/executor.py`
> **更新日期**：2026-08-27
> **职责**：`ReActAgent`——把 ReAct 推理策略编排进 `BaseAgent` 生命周期的具体 Agent 类型
> **状态**：✅ 已实现
> **配套**：算法实现见 [react.md](../reasoning_doc/react.md)（`ReActStrategy`）

---

## 📋 目录

- [定位与职责](#定位与职责)
- [接口契约](#接口契约)
- [行为边界](#行为边界)
- [使用示例](#使用示例)
- [设计决策](#设计决策)
- [测试](#测试)
- [相关文档](#相关文档)

---

## 定位与职责

`ReActAgent` 是 ReAct 策略的 **agent/ 编排载体**——桥接 `ReActStrategy`（原子算法）到 `BaseAgent`（生命周期框架）：

1. **可实例化的编排类型**：`BaseAgent` 是抽象类（`_strategy_cycle` 为抽象方法），`ReActAgent` 实现之，是应用层可构造、可运行的 ReAct Agent 类型
2. **生命周期接入**：继承 `BaseAgent.run()` 的状态机 / 异常处理 / SSE 事件路由 / 结果存储
3. **策略适配**：从 `AgentContext` 解出运行参数传给策略（策略不依赖 AgentContext）；把策略产出 `ReActOutcome` 组装为 `AgentResult`
4. **兼容 API 面**：`_execute_tool_calls` 转发到策略原语，保持既有测试与调用方零断裂

> 本组件只做桥接，不承载 ReAct 算法；算法在 `reasoning/react.py`。

---

## 接口契约

### `ReActAgent(BaseAgent)`

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(llm: LLMGateway, tools: ToolGateway)` | 构造 `ReActStrategy(llm, tools)` |
| `_strategy_cycle` | `(user_input, messages) -> AsyncGenerator[str]` | 委托 `strategy.execute(...)`，结束后 `_map_outcome` 组装 `AgentResult` |
| `_execute_tool_calls` | `(tool_calls, messages, iteration) -> AsyncGenerator[str]` | 转发到 `strategy.execute_tool_calls`（工具并行执行原语） |
| `_map_outcome` | `(outcome: ReActOutcome \| None) -> AgentResult` | 策略产出 → AgentResult 契约转换 |

**对外 API（继承自 `BaseAgent`）**：`run(user_input, messages, context)`（流式 SSE）、`state`、`result`——与抽离前完全一致。

**依赖方向**：`agent → reasoning`（编排调用策略）；策略层不反向依赖 agent/。

---

## 行为边界

| 场景 | 处理 |
| --- | --- |
| `AgentContext` 未设置 | `_strategy_cycle` 抛 `RuntimeError` |
| 策略未产出结果（`outcome is None`） | `_map_outcome` 返回失败 `AgentResult`（`success=False` + `error`） |
| LLM 调用失败 | 经策略短路返回失败结果（LLM-001，见 react.md 行为边界） |
| 用户取消 | `BaseAgent.run()` 捕获 `CancelledError` → `state=CANCELLED` |

---

## 使用示例

```python
from app.domain.agent import AgentContext, ReActAgent

agent = ReActAgent(llm=llm_service, tools=tool_service)
ctx = AgentContext(session_id="sess_001", user_id="user_001")
async for event in agent.run("查询良率", messages, ctx):
    yield event  # 转发 SSE 事件
result = agent.result  # AgentResult
```

---

## 设计决策

- **为什么桥接而非在 executor 内嵌算法**：遵循 agent/（编排）+ reasoning/（实现）边界，使 ReAct 算法可被 PlannerAgent / ReflectionAgent 复用，策略可独立测试
- **为什么保留 `_execute_tool_calls` 转发方法**：既有测试（test_agent.py）直接调用该方法，转发保证零断裂；也是编排复用策略原语的入口
- **决策记录**：[ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)

---

## 测试

`tests/unit/test_agent.py`（3 用例）：

- `test_execute_tool_calls_parallel_preserves_order` — 工具消息顺序 = 输入顺序（gather 保序）
- `test_execute_tool_calls_parallel_actually_concurrent` — 并行执行耗时 < 串行和
- `test_strategy_cycle_short_circuits_on_llm_error` — LLM 失败短路返回失败结果（LLM-001）

策略算法本身由 `tests/unit/test_react_strategy.py`（6 用例）覆盖，见 [react.md](../reasoning_doc/react.md)。

---

## 相关文档

- [Agent 模块对外接口文档](agent.md)（主文档）
- [ReActStrategy 策略组件](../reasoning_doc/react.md)（同级组件）
- [推理策略模块](../reasoning_doc/reasoning.md)
- [领域层说明](../README.md)
- [ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)
