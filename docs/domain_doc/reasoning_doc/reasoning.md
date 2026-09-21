# 推理策略模块对外接口文档

> **对应代码**：`app/domain/reasoning/`
> **更新日期**：2026-09-16
> **文档定位**：推理策略模块对外接口文档——策略类契约 + 内部组件导航；服务对象为 agent/ 层编排（ReActAgent / PlannerAgent / ReflectionAgent）
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。边界：（react.py ✅；reflection.py ✅；planner.py ✅；包内纯转换 ✅；chain_of_thought 预留）

---

## 📋 目录

- [推理策略模块对外接口文档](#推理策略模块对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [对外接口](#对外接口)
  - [内部实现组织](#内部实现组织)
  - [Reflection 设计启示（已落地）](#reflection-设计启示已落地)
  - [CoT 预留](#cot-预留)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

推理策略模块保存**可独立复用的领域推理流程**。它们由 agent/ 桥接到统一生命周期，也可在
reasoning 内部组合；agent/ 管运行入口、上下文与结果映射，reasoning/ 管策略阶段、调用检查点、
成果接管和策略终态：

- **ReAct**：推理 ↔ 工具循环（已实现）
- **Reflection**：生成 → 自查 → 修正（已实现，证据链语义自查）
- **Planner**：Plan-then-Execute 单 Agent 编排（已实现：规划 → 逐步骤执行 → 证据链汇总）
- **CoT**：纯推理引导（预留）

### 模块结构

```text
app/domain/reasoning/
├── __init__.py          # 子包导出（策略、Outcome 与执行参数值对象）
├── execution.py         # 六类不可变执行参数值对象
├── _common.py           # 策略共享小工具（GuardResult / evaluate_guard / dispatch_error / merge_usage）
├── _planner_steps.py    # Planner 步骤、计划快照与审计记录纯转换
├── _react_protocol.py   # ReAct final_answer、调用身份与动作指纹纯协议转换
├── react.py             # ReAct 推理（ReActStrategy + ReActOutcome，✅）
├── reflection.py        # Reflection 推理（ReflectionStrategy + ReflectionOutcome，✅）
├── planner.py           # Planner 推理（PlannerStrategy + PlannerOutcome，✅）
└── chain_of_thought.py  # CoT 推理（预留）
```

### 设计原则

1. **独立领域流程**：每个已实现策略可独立跑通，也可组合其他策略；不持有 Agent 生命周期状态
2. **显式组合**：Planner / Reflection 复用完整 ReAct 流程；工具执行等较小原语保留明确的直接入口
3. **契约随策略发布**：策略级 Schema / 结果载体随策略模块发布，编排方引用

### 依赖关系

```text
BaseAgent._strategy_cycle()  ← 策略接口（agent/ 层）
    ├── ReActStrategy（✅ react.py；executor.py 桥接）
    ├── ToolBatchCollector（✅ tool_batch.py；批次事实快照）
    ├── ReflectionStrategy（✅ reflection.py；agent/reflection.py 桥接）
    ├── PlannerStrategy（✅ planner.py；agent/planner.py 桥接）
    ├── Chain-of-Thought（预留，本模块）
    └── ...
```

**依赖方向**：`reasoning/` 只依赖 ports + shared + prompts（提示词组装；不 import `agent/`）。
agent/ 桥接把 `AgentContext` 显式映射为 `execution.py` 的六类语义值对象，reasoning 不读取
Agent 生命周期上下文。

---

## 对外接口

> 对外接口 = 被 agent/ 层编排依赖的策略类和执行参数值对象。三种策略的 `execute()` 接收
> `ReasoningRunScope`、`ModelOptions`、`ExecutionLimits`、`ContextWindowLimits`、
> `RecoveryBudget`、`ToolExecutionOptions`；业务输入、可变消息及策略专属输出参数保持显式。

| 策略类 | 契约（方法） | 状态 | 说明 |
| --- | --- | --- | --- |
| `ReActStrategy` | `execute(...)` / `execute_tool_calls(...)` + `outcome` | ✅ | 完整契约见 [react.md](react.md) |
| `ReflectionStrategy` | `execute(...)` + `outcome` | ✅ | 完整契约见 [reflection.md](reflection.md) |
| `PlannerStrategy` | `execute(...)` + `outcome` | ✅ | 完整契约见 [planner.md](planner.md) |
| CoT | 预留 | ⬜ | — |

被编排方式：`ReActAgent._strategy_cycle()` 委托 `ReActStrategy.execute()`；`ReflectionAgent._strategy_cycle()` 委托 `ReflectionStrategy.execute()`；`PlannerAgent._strategy_cycle()` 委托 `PlannerStrategy.execute()`（其每步执行再复用内部 `ReActStrategy.execute` 做被步骤约束的 agent 循环，见 [planner.md](planner.md)）。`execute_tool_calls()` 是不承担完整策略级 Guard 与终态的工具并行原语，仍执行调用身份检查并接收取消、期限和清理控制；它只在 reasoning 策略边界公开，不经 ReActAgent 重复转发。

---

## 内部实现组织

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| [react.md](react.md) | `react.py` | ReAct 推理（ReActStrategy + ReActOutcome） | [见对齐表](../../ALIGNMENT.md) |
| [reflection.md](reflection.md) | `reflection.py` | Reflection 推理（生成 → 自查 → 修正） | [见对齐表](../../ALIGNMENT.md) |
| [planner.md](planner.md) | `planner.py` | Planner 推理（规划 → 逐步骤执行 → 汇总，Plan-then-Execute） | [见对齐表](../../ALIGNMENT.md) |
| [planner.md](planner.md) | `_planner_steps.py` | Planner 步骤规范化、计划快照和单步审计记录纯转换 | [见对齐表](../../ALIGNMENT.md) |
| [react.md](react.md) | `_react_protocol.py` | ReAct 终止工具、调用身份与动作指纹纯协议转换 | [见对齐表](../../ALIGNMENT.md) |
| [_common.md](_common.md) | `_common.py` | 类型化执行护栏、错误分发与 usage 合并（无状态共享） | [见对齐表](../../ALIGNMENT.md) |
| 执行参数值对象 | `execution.py` | 按运行身份、模型、执行限制、上下文、恢复预算和工具执行六种变化原因组织参数 | [见对齐表](../../ALIGNMENT.md) |
| chain_of_thought.py | `chain_of_thought.py` | CoT 推理（纯推理引导） | [见对齐表](../../ALIGNMENT.md) |

`RecoveryBudget.max_replan_rounds` / `max_refine_rounds` 为 `None` 表示当前策略不适用，
`0` 表示适用但不允许对应恢复动作。值对象冻结字段引用，但其中的 `asyncio.Event` 仍由
既有 Owner 设置。运行作用域在构造时校验非空身份与 Event 控制链，恢复预算拒绝负数，
执行限制拒绝 0 或负的轮次与重复动作上限、以及非有限正数的批次收尾窗口——这三者的
配置侧口径分别是 1-100、>= 1 与有限正数，越界会让循环零执行却产出正常终态，故在
副作用前拒绝。`ExecutionLimits.max_execution_time` 不在此列：配置侧无对应口径，领域侧
按 `max(0.0, ...)` 处理负值。Planner 子 ReAct 复用同一组对象，只通过
`dataclasses.replace` 更新剩余墙钟。

---

## Reflection 设计启示（已落地）

> 依据：learning-agent 项目的 Reflection 实验（批量转换任务 × 注入语义错误 × 对照）。启示已全部落地——程序校验查形状、模型自查查语义的分工与边界、自查清单穷举、地面真值兜底等设计要点逐条见 [reflection.md](reflection.md)（grounding / 自查清单 / 修正兜底）；工业级对标见 [reflection_benchmark.md](reflection_benchmark.md)。

## CoT 预留

`chain_of_thought.py` 为预留空壳。CoT 是纯推理引导（不调用工具），当前不服务主链路（RCA 排查是工具驱动），按「不做或预留」原则降级，出现真实需求再落地。

---

## 相关文档

- [领域推理纯边界 ADR](../../../adr/domain/reasoning/2026-09-16-strategy-pure-boundaries.md)
- [ReActStrategy 策略组件](react.md) + [ReAct 工业级对标基准](react_benchmark.md)
- [ReflectionStrategy 策略组件](reflection.md) + [Reflection 工业级对标基准](reflection_benchmark.md)
- [PlannerStrategy 策略组件](planner.md) + [Planner 工业级对标基准](planner_benchmark.md)
- [共享小工具 _common](_common.md)
- [领域层说明](../README.md)
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 [ReActAgent 桥接组件](../agent_doc/executor.md)）
- [架构设计](../../project/architecture.md)
