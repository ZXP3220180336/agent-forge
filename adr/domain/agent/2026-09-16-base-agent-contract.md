# BaseAgent 只保留已兑现的运行契约

日期：2026-09-16。决定状态：已接受。实现状态：已验证。范围：`BaseAgent` 公共状态、扩展面与
三个 Agent 桥接。关联：[AGENT-001](../../../issues/domain/agent/2026-09-16-base-contract-drift.md)。

## 背景

`BaseAgent` 公开了 `AgentState.WAITING`，并在正式说明中把四个 `on_*` no-op 方法列为可覆盖的
生命周期钩子。然而当前状态机从不进入 `WAITING`，Runner 也从不调用这些方法；覆盖它们不会收到
回调。基类还维护了无消费者的 `_tool_call_history`。这些接口会让调用方把未兑现的占位误认为稳定
能力。

可选方案有三种：保留占位以维持表面兼容；补齐完整状态转换和 Hook 调度；删除虚假契约，仅保留
实际运行面。第一种继续传播错误承诺；第二种没有当前产品调用方，并会引入新的调用顺序、异常和
资源语义。本次采用第三种。

工业参照中，[OpenAI Agents SDK 生命周期钩子](https://openai.github.io/openai-agents-python/ref/lifecycle/)
以显式 Hook 对象和 Runner 调用点提供回调，而 Python 官方
[`abc.abstractmethod`](https://docs.python.org/3/library/abc.html#abc.abstractmethod) 用于表达子类必须实现的
契约。没有调用点的具体 no-op 方法不构成可工作的 Hook 系统。

## 决定

- 删除未接线的 `on_thought`、`on_tool_call`、`on_tool_result`、`on_complete`。
- 删除无转换路径的 `AgentState.WAITING` 和无消费者的 `_tool_call_history`。
- 保留 `_strategy_cycle` 作为唯一抽象扩展契约；新增受保护的 `_require_context()`，统一桥接取得当前
  `AgentContext` 的前置检查。
- 删除 `ReActAgent._execute_tool_calls()` 转发；并行与保序直接由策略原语及其测试负责。
- 不把 reasoning 的 Guard、usage、重试、成果接管或终态上移到 BaseAgent，也不新增通用执行模板。

## 后果与兼容边界

当前仓库没有被删除成员的消费者，三种 Agent 的运行、事件和结果行为保持不变。仓库外代码若直接
引用 `WAITING`、调用或反射检查四个 no-op 方法，会收到枚举或属性不存在的错误；本项目当前不承诺
作为第三方 Python Agent SDK 提供兼容性，因此不保留无法兑现的兼容壳。

`_execute_tool_calls()` 是受保护方法，仓库内只有重复单元测试调用；删除后调用方应直接使用
`ReActStrategy.execute_tool_calls()`。原 ReAct 策略抽离 ADR 中“待策略调用面稳定后评估移除”的
升级条件已满足。

如果未来出现真实生命周期回调需求，应先定义 Hook 对象、调用时点、上下文、异常隔离和异步顺序，
再作为新契约引入；如果需要暴露工具等待状态，应先建立可验证的状态转换和并发可见性语义。

## 验证

全仓检索确认被删除成员没有生产或测试消费者；三个桥接主循环的 `_require_context()` 替换保持原
异常类型和文案。
基类契约整理时定向回归 50 项、全量回归 1248 项通过；删除转发和两项重复测试后，最终定向回归
25 项、全量回归 1246 项通过。测试总数减少 2 项与重复测试删除一致；策略层并行与保序测试仍在
最终定向及全量套件中通过。ALIGNMENT 校验、`app`/`tests` 编译及差异格式检查通过。
