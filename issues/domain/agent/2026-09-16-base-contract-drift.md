# AGENT-001：BaseAgent 扩展契约与实际运行脱节

状态：已修复。优先级：P3。发现来源：三策略 `agent/` / `reasoning/` 分层审核。范围：BaseAgent、
三种 Agent 桥接及当前模块说明。

## 现象与影响

`BaseAgent` 文档宣称通过不存在的 `_emit_event()` 解耦事件，并提供四个可覆盖 `on_*` 生命周期钩子；
全仓没有钩子调用点，覆盖它们不会获得回调。`_tool_call_history` 只初始化和重置，没有消费者；
`AgentState.WAITING` 也没有合法转换路径，但模块文档把它画入当前状态机。上述声明会让调用方误判
可用扩展点和运行状态，增加维护者在错误边界上继续建设的风险。

## 根因与方案

早期 BaseAgent 为未来扩展预留接口，后续实际工具事件、事实和状态均由可独立复用的 reasoning 策略
管理，但基类占位没有随实现收敛。[OpenAI Agents SDK 生命周期钩子](https://openai.github.io/openai-agents-python/ref/lifecycle/)
通过显式 Hook 对象和 Runner 调用点兑现回调；Python 官方
[`abc.abstractmethod`](https://docs.python.org/3/library/abc.html#abc.abstractmethod) 则表达必须由子类实现的
接口。本项目没有真实钩子消费者，因此删除未接线的具体 no-op 方法和冗余状态，保留唯一必要的
抽象 `_strategy_cycle`。

三个桥接重复的“取得当前 AgentContext，否则失败”具有同一前置语义，提取为 BaseAgent 受保护方法
`_require_context()`；策略参数继续显式映射。Guard、usage、fallback、终态和策略专用预算仍由
reasoning 持有，不建立通用执行模板或无类型参数包。

## 实施与验证

已删除四个未接线钩子、无消费者的 `_tool_call_history` 和无转换路径的 `WAITING`，并以
`_require_context()` 收敛三个 Agent 桥接主循环的相同前置检查。同步修正模块说明、状态图和
Planner/Reflection 对 ReAct 的真实调用关系。仓库检索确认已删除符号没有剩余生产或测试消费者。
后续复核又确认 `ReActAgent._execute_tool_calls()` 只有两项重复测试调用，故删除该转发并由既有
`ReActStrategy.execute_tool_calls()` 测试继续保护并行与保序行为。

基类契约整理时定向回归 50 项、全量回归 1248 项通过；删除转发和两项重复测试后，最终定向回归
25 项、全量回归 1246 项通过（仅 1 项既有 Starlette/httpx 弃用提示）。策略层并行与保序测试仍
通过；ALIGNMENT 校验、`app`/`tests` 编译和差异格式检查通过。实施范围与评审见
[R-02 计划](../../../docs/todo.md)。

边缘情况：若仓库外调用方曾依赖这些未接线的具体方法或 `WAITING` 枚举成员，本次会暴露为导入或
属性错误；当前项目不是对外发布的 Agent SDK，仓库内无消费者，因此不保留无法兑现的兼容壳。

## 可复用教训

预留方法、枚举成员和文档中的扩展点都会形成可观察契约。只有存在真实调用点或状态转换时才应将其
列为当前能力；未来能力先记录升级条件，待运行语义确定后再加入公共接口。

## 关联记录

- [ReAct 策略抽离 ADR](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)
- [BaseAgent 契约收敛 ADR](../../../adr/domain/agent/2026-09-16-base-agent-contract.md)
- [Planner 两层结构 ADR](../../../adr/domain/reasoning/2026-09-05-planner-strategy.md)
- [Agent 模块说明](../../../docs/domain_doc/agent_doc/agent.md)
