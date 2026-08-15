# 推理策略说明文档

> **更新日期**：2026-08-03
> **模块**：`app/domain/reasoning/`
> **实现状态**：❌ 预留（全部文件为空）
> **架构定位**：核心层的推理策略实现，作为 `BaseAgent._strategy_cycle()` 的候选策略

---

## 📋 目录

- [模块概述](#模块概述)
- [实现状态总览](#实现状态总览)
- [规划结构](#规划结构)
- [与 Agent 层的关系](#与-agent-层的关系)
- [相关文档](#相关文档)

---

## 模块概述

推理策略模块是核心层的**策略库**，为 Agent 提供多种推理方式的实现。当前 Agent 的 ReAct 策略直接实现在 `app/domain/agent/executor.py` 中；本模块规划将这些推理方式**抽离为独立策略**，未来可作为 `_strategy_cycle()` 的替代实现。

```
BaseAgent._strategy_cycle()  ← 策略接口
    ├── ReAct（当前，实现在 agent/executor.py）
    ├── Chain-of-Thought（规划，本模块）
    ├── Reflection（规划，本模块）
    └── ...
```

---

## 实现状态总览

| 文件 | 状态 | 定位 |
| --- | --- | --- |
| `__init__.py` | ❌ 空 | 子包入口 |
| `chain_of_thought.py` | ❌ 空 | 思维链（Chain-of-Thought）推理 |
| `react.py` | ❌ 空 | ReAct 推理（当前逻辑在 agent/executor.py 中） |
| `reflection.py` | ❌ 空 | 反思（Reflection）推理 |

**当前状态**：全部为预留空文件，无任何实现。

---

## 规划结构

### `chain_of_thought.py` — 思维链（Chain-of-Thought）

- **思路**：引导模型分步推理（"让我们一步步思考"），提升复杂推理的准确性
- **适用**：数学推理、多步逻辑分析
- **与 ReAct 区别**：CoT 不调用工具，是纯推理路径；ReAct 是推理 ↔ 工具交替

### `react.py` — ReAct 推理

- **思路**：推理（Reason）→ 行动（Act）→ 观察（Observe）循环
- **现状**：ReAct 逻辑已实现在 `agent/executor.py` 的 `ReActAgent` 中
- **规划**：抽离为独立策略类，作为 `_strategy_cycle()` 的复用实现

### `reflection.py` — 反思（Reflection）

- **思路**：生成 → 反思 → 修正，模型自我评估输出质量并改进
- **适用**：代码生成（自动检查 bug）、长文写作（质量改进）
- **工作流**：
  1. 生成阶段：LLM 首轮输出
  2. 反思阶段：LLM 评估输出质量，指出问题
  3. 修正阶段：根据反思结果修正
  4. 验证阶段：再次评估，确认达到标准

---

## 与 Agent 层的关系

| 层 | 职责 |
| --- | --- |
| Agent 层（`agent/`） | 策略**编排**：`BaseAgent.run()` 统一入口 + `_strategy_cycle()` 策略接口 |
| 推理层（本模块，`reasoning/`） | 策略**实现**：具体的推理算法（CoT / ReAct / Reflection） |

当前 `ReActAgent` 把编排与 ReAct 策略耦合在 `executor.py` 中。规划方向是：`reasoning/` 提供策略实现，Agent 层通过 `_strategy_cycle()` 选择策略——实现"策略模式"的解耦目标（`agent.md` 中设计的 PlannerAgent / ReflectionAgent 预留）。

---

## 相关文档

- [领域层说明](../README.md)
- [Agent 模块详解](../agent_doc/agent.md)
- [架构设计](../../architecture.md)
