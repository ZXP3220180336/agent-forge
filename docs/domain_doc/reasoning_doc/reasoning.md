# 推理策略模块对外接口文档

> **对应代码**：`app/domain/reasoning/`
> **更新日期**：2026-08-27
> **文档定位**：推理策略模块对外接口文档——策略类契约 + 内部组件导航；服务对象为 agent/ 层编排（ReActAgent / PlannerAgent / ReflectionAgent）
> **实现状态**：🔶 部分实现（react.py ✅；reflection.py ✅；chain_of_thought 预留）

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

推理策略模块是领域层的**原子推理策略库**，为 Agent 提供推理方式实现，被 agent/ 层编排调用（agent/ 管策略编排与生命周期，reasoning/ 管策略实现）：

- **ReAct**：推理 ↔ 工具循环（已实现）
- **Reflection**：生成 → 自查 → 修正（已实现，证据链语义自查）
- **CoT**：纯推理引导（预留）

### 模块结构

```text
app/domain/reasoning/
├── __init__.py          # 子包导出（ReActStrategy / ReActOutcome）
├── react.py             # ReAct 推理（ReActStrategy + ReActOutcome，✅）
├── reflection.py        # Reflection 推理（ReflectionStrategy + ReflectionOutcome，✅）
└── chain_of_thought.py  # CoT 推理（预留）
```

### 设计原则

1. **原子策略**：每个文件是一个可独立跑通的推理算法，不持有 Agent 状态
2. **可复用原语**：策略内提供工具执行等原语，供不同编排复用
3. **契约随策略发布**：策略级 Schema / 结果载体随策略模块发布，编排方引用

### 依赖关系

```text
BaseAgent._strategy_cycle()  ← 策略接口（agent/ 层）
    ├── ReActStrategy（✅ 本模块 react.py；executor.py 桥接）
    ├── Reflection（预留，本模块）
    ├── Chain-of-Thought（预留，本模块）
    └── ...
```

**依赖方向**：`reasoning/` 只依赖 ports + shared（不 import `agent/`，策略收标量参数而非 AgentContext），被 agent/ 层编排调用。

---

## 对外接口

> 对外接口 = 被 agent/ 层编排依赖的策略类。策略收标量参数，经 `outcome` / 返回暴露结果。

| 策略类 | 契约（方法） | 状态 | 说明 |
| --- | --- | --- | --- |
| `ReActStrategy` | `execute(...)` / `execute_tool_calls(...)` + `outcome` | ✅ | 完整契约见 [react.md](react.md) |
| `ReflectionStrategy` | `execute(...)` + `outcome` | ✅ | 完整契约见 [reflection.md](reflection.md) |
| CoT | 预留 | ⬜ | — |

被编排方式：`ReActAgent._strategy_cycle()` 委托 `ReActStrategy.execute()`；`execute_tool_calls()` 供 PlannerAgent 执行阶段 / ReflectionAgent 收集阶段复用；`ReflectionAgent._strategy_cycle()` 委托 `ReflectionStrategy.execute()`。

---

## 内部实现组织

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| [react.md](react.md) | `react.py` | ReAct 推理（ReActStrategy + ReActOutcome） | ✅ |
| [reflection.md](reflection.md) | `reflection.py` | Reflection 推理（生成 → 自查 → 修正） | ✅ |
| chain_of_thought.py | `chain_of_thought.py` | CoT 推理（纯推理引导） | ⬜ 预留 |

---

## Reflection 设计启示（已落地）

> 依据：learning-agent 项目的 Reflection 实验（批量转换任务 × 注入语义错误 × 对照），验证"程序校验查形状 vs 模型自查查语义"的分工与边界。启示已全部落地到 [reflection.md](reflection.md)（grounding 证据锚定 / 自查清单穷举 / 地面真值兜底）与 [reflection_benchmark.md](reflection_benchmark.md)（工业级对标）。

1. **程序校验与模型自查分工互补**：程序校验（jsonschema）查形状，模型自查查语义——`REFLECTION_SCHEMA` / `CRITIQUE_SCHEMA` 程序校验形状，自查清单补语义。
2. **自查范围 = 提示词清单（Scope 盲区）**：`CRITIQUE_PROMPT` 穷举 9 个核对维度（grounding / consistency / fabrication / attribution / confidence / evidence_gap / completeness / internal_consistency / next_steps），清单为唯一自查范围。
3. **自查多报无害、漏报危险**：critic 输出结构化 issues，修正环节兜底（不盲目采纳）。
4. **能规则化的交给规则**：形状 / 值域交给 jsonschema，语义交给自查。
5. **修正用地面真值兜底**：`REFINE_PROMPT` 基于证据链重写，与证据链矛盾的 issues 不采纳。

## CoT 预留

`chain_of_thought.py` 为预留空壳。CoT 是纯推理引导（不调用工具），当前不服务主链路（RCA 排查是工具驱动），按「不做或预留」原则降级，出现真实需求再落地。

---

## 相关文档

- [ReActStrategy 策略组件](react.md)
- [ReAct 工业级对标基准](react_benchmark.md)（能力基准与差距清单）
- [领域层说明](../README.md)
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 [ReActAgent 桥接组件](../agent_doc/executor.md)）
- [架构设计](../../architecture.md)
