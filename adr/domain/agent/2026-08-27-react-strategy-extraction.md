# ReAct 策略抽离到 reasoning/（策略模式解耦）

> 日期：2026-08-27 ｜ 层级：domain/agent + domain/reasoning

## Context

- ReAct 循环逻辑完整内嵌在 `app/domain/agent/executor.py` 的 `ReActAgent._strategy_cycle` 中，agent/ 层同时承担「策略编排（BaseAgent 生命周期）」与「策略实现（ReAct 算法）」双重职责。
- `app/domain/reasoning/`（策略库）全部空壳，规划目标（reasoning.md）是「策略实现，被 agent/ 编排调用」。
- 需求：PlannerAgent 执行阶段、ReflectionAgent 收集阶段需要复用工具执行循环；策略应可独立测试、可被任意编排复用。

## Decision

1. **ReActStrategy 抽离到 `reasoning/react.py`**：纯算法类（构造注入 `LLMGateway` / `ToolGateway`），`execute()` 承载完整 ReAct 主循环（finish_reason 分支 / LLM 失败短路 / 空输出重试 / 迭代兜底），yield SSE 事件，结果写入 `ReActOutcome`；`execute_tool_calls()` 为工具并行执行原语（gather 保序），供 ReAct 循环与 PlannerAgent / ReflectionAgent 复用。
2. **`executor.py` 变薄桥接**：`ReActAgent` 保留 `_strategy_cycle` 委托策略 execute、`_map_outcome` 组装 AgentResult、`_execute_tool_calls` 转发到策略原语（既有 test_agent.py 直接调用该方法，转发保持零改动）。对外 API（run/result/state）与事件流不变。
3. **策略收标量参数，不依赖 agent/**：`execute()` 接收 `max_iterations` / `temperature` / `max_tokens` 标量而非 `AgentContext`——遵守依赖方向 `reasoning → ports + shared`（不 import `agent/`），策略可独立测试。
4. **边界准则**：reasoning/ 收原子推理算法（ReAct / Reflection / CoT），agent/ 收策略编排 + 生命周期（BaseAgent 骨架 + PlannerAgent / ReflectionAgent 组装）。Plan-then-Execute 归 agent/（工业实证：planning 是编排职责，非原子算法）。

## Consequences

- ✅ 行为不变：ReAct 抽离后全量 589 测试通过（含既有 test_agent.py 3 用例零改动、test_chat_flow 集成链路）。
- ✅ 策略可独立测试、可复用：`ReActStrategy.execute_tool_calls` 成为 PlannerAgent 执行阶段 / ReflectionAgent 收集阶段的共享原语。
- ✅ 依赖方向正确：`reasoning/` 只依赖 ports + shared，被 agent/ 编排调用。
- ⚠️ `ReActAgent` 保留 `_execute_tool_calls` 转发方法：为兼容既有测试的桥接面，未来若策略调用面稳定可评估是否移除。
- 📌 后续：CoT / Reflection 策略沿用同一边界（CoT 预留见 reasoning ADR）。
