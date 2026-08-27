# 推理策略模块对外接口文档

> **对应代码**：`app/domain/reasoning/`
> **更新日期**：2026-08-27
> **文档定位**：推理策略模块对外接口文档——策略类契约 + 内部组件导航；服务对象为 agent/ 层编排（ReActAgent / PlannerAgent / ReflectionAgent）
> **实现状态**：🔶 部分实现（react.py ✅；reflection / chain_of_thought 预留）

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
  - [预留策略设计启示](#预留策略设计启示)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

推理策略模块是领域层的**原子推理策略库**，为 Agent 提供推理方式实现，被 agent/ 层编排调用（agent/ 管策略编排与生命周期，reasoning/ 管策略实现）：

- **ReAct**：推理 ↔ 工具循环（已实现）
- **Reflection**：生成 → 自查 → 修正（预留）
- **CoT**：纯推理引导（预留）

### 模块结构

```text
app/domain/reasoning/
├── __init__.py          # 子包导出（ReActStrategy / ReActOutcome）
├── react.py             # ReAct 推理（ReActStrategy + ReActOutcome，✅）
├── reflection.py        # Reflection 推理（预留）
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
| `ReflectionStrategy` | 预留 | ⬜ | 设计启示见下节 |
| CoT | 预留 | ⬜ | — |

被编排方式：`ReActAgent._strategy_cycle()` 委托 `ReActStrategy.execute()`；`execute_tool_calls()` 供 PlannerAgent 执行阶段 / ReflectionAgent 收集阶段复用。

---

## 内部实现组织

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| [react.md](react.md) | `react.py` | ReAct 推理（ReActStrategy + ReActOutcome） | ✅ |
| reflection.py | `reflection.py` | Reflection 推理（生成 → 自查 → 修正） | ⬜ 预留 |
| chain_of_thought.py | `chain_of_thought.py` | CoT 推理（纯推理引导） | ⬜ 预留 |

---

## 预留策略设计启示

> 依据：learning-agent 项目的 Reflection 实验（批量转换任务 × 注入语义错误 × 对照），验证"程序校验查形状 vs 模型自查查语义"的分工与边界。此处只留工业级结论，实验细节见该学习项目。供 Reflection 策略实现（Slice 4）参考。

1. **程序校验与模型自查分工互补**：程序校验（jsonschema）查**形状**（字段 / 类型 / 值域），确定性、便宜；模型自查查**语义**（配对正确、数值精确），概率性、多一次 LLM 调用。程序校验查不了"配对是否正确"——4 项都在、类型全对、结果填错位置照样放行，自查能补。
2. **自查范围 = 提示词清单（Scope 盲区）**：审查指令只查列出的维度，清单外的必漏——实验实证 `answer` 与 `results` 自相矛盾（清单外），自查直接放行。设计自查提示词要**穷举要查的维度**（字段间一致性、单位方向、证据支撑…），漏一个就是漏报温床。这也是独立 Critic Agent（不同上下文 / 视角）存在的理由——同模型自查被指令范围锁死。
3. **自查是概率性的，精度取决于错误类型**：客观对照类（数值偏差 / 数量 / 重复）抓得准但报告索引可能错位；语义配对类（互换 / 单位错位）会多报（false positive）。多报无害（修正兜底），**漏报危险**。
4. **能规则化的交给规则**：jsonschema 能拦截的（`maxItems` / `minItems` / `pattern` / `enum`）尽量规则化，不要押给概率性自查。
5. **修正环节用地面真值兜底**：Refine 基于完整上下文（工具记录 / 真实结果）重写，不盲目采纳 issues——自查的误差被修正兜底。**自查不完美，但错了也无害**，前提是修正有真实依据。

---

## 相关文档

- [ReActStrategy 策略组件](react.md)
- [领域层说明](../README.md)
- [Agent 模块对外接口文档](../agent_doc/agent.md)（含 [ReActAgent 桥接组件](../agent_doc/executor.md)）
- [架构设计](../../architecture.md)
