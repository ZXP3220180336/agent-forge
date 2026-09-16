# Reasoning 执行参数按语义分组

日期：2026-09-16。决定状态：已接受。实现状态：已验证。
范围：ReAct、Planner、Reflection 的公开执行入口及 Agent 桥接。

## 背景

三种策略的 `execute()` 入口逐步累积到二十余个标量参数。每增加一种运行控制、预算或工具限制，
三个 Agent 桥接、Planner/Reflection 的嵌套 ReAct 调用和大量直接测试都要同步修改。参数虽然共享，
变化原因并不相同：运行身份与取消传播属于一次 run，模型参数属于调用选择，重试与重规划属于恢复
预算。继续使用平铺标量会扩大修改面，也容易在嵌套调用中遗漏某一项。

真实备选如下：

1. 保留标量签名。没有新增类型，但无法解决签名扩张和调用点漂移。
2. 建立一个扁平 `ReasoningExecutionContext`。调用点最短，但会把运行身份、模型、预算和工具执行
   混成新的万能参数袋，字段所有权和适用范围仍不清晰。
3. 按变化原因建立多个不可变参数对象。调用方仍需显式组装，但新增字段只影响对应语义组，且类型名
   能直接表达用途，因此采用该方案。

工业参照采用 Martin Fowler 的
[Introduce Parameter Object](https://refactoring.com/catalog/introduceParameterObject.html)：把经常一起出现的
参数组合为对象。本项目进一步按变化原因拆成六组，避免把所有参数合并为单一上下文。

## 决定

- `execution.py` 定义 `ReasoningRunScope`、`ModelOptions`、`ExecutionLimits`、
  `ContextWindowLimits`、`RecoveryBudget`、`ToolExecutionOptions`，全部使用
  `@dataclass(frozen=True, slots=True)`。
- `ReasoningRunScope` 在构造时校验非空 run 身份与 Event 控制链，保证 Planner/Reflection
  直接调用也在任何模型或工具副作用前失败；`RecoveryBudget` 拒绝所有负预算。
- 三种策略的 `execute()` 接收上述六类值对象。`user_input`、可变 `messages`、`output_schema`、
  `baseline_usage` 和 `stream_mode` 继续显式出现，因为它们是本次业务载荷或策略入口选择，并非共享配置组。
- `RecoveryBudget` 同时承载通用重试、Planner replan 和 Reflection refine 次数。
  `max_replan_rounds` / `max_refine_rounds` 为 `None` 表示当前策略不适用，`0` 表示适用但没有对应恢复次数；
  Planner/Reflection 缺少自身预算时在任何外部调用前抛 `ValueError`。
- 三个 Agent 桥接负责把 `AgentContext` 显式映射为六类对象，保持 `agent → reasoning` 单向依赖。
  Planner 将现有 `ctx.max_refine_rounds` 映射到 replan 字段，Reflection 映射到 refine 字段。
- Planner 的子 ReAct 复用父运行作用域和其余语义对象，仅用 `dataclasses.replace` 写入当前剩余墙钟。
- 不保留旧标量签名或 `**kwargs` 兼容入口。测试侧构造器只负责测试场景装配，不进入生产路径。

## 后果与边界

- 收益：后续增加模型、上下文或恢复参数时，签名和调用点保持稳定；嵌套策略透传关系可按对象审查；
  Agent 生命周期上下文不会渗入 reasoning。
- 代价：调用方需要构造六个对象，简单直接调用比标量写法多几行；对象冻结的是字段引用，
  `asyncio.Event` 本身仍可由原 Owner 设置，这是取消传播所需的浅不可变语义。
- 本决定不改变任何 Guard 优先级、预算计数时点、usage 口径、工具执行语义或终态，也不把
  `execute_tool_calls()` 原语改为接收这些策略级对象。
- 若某一参数组未来取得独立行为或生命周期，再评估提取服务或 Policy；仅字段数量增长不触发升级。

## 实施与验证

生产调用点和直接策略测试均已迁移；新增冻结、Event 引用、运行作用域校验、负预算、`None/0`
区分以及策略缺失专属预算测试。
定向与全量验证结果记录在 [R-03 计划](../../../docs/todo.md#r-03-reasoning-execution-parameters)。

## 关联记录

- [REASON-026](../../../issues/domain/reasoning/2026-09-16-execute-parameter-drift.md)
- [推理策略模块](../../../docs/domain_doc/reasoning_doc/reasoning.md)
- [领域推理纯边界 ADR](2026-09-16-strategy-pure-boundaries.md)
