# 推理策略说明文档

> **更新日期**：2026-08-26
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

```text
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

#### 设计启示（来自 Agent 行为实验）

> 依据：learning-agent 项目的 Reflection 实验（批量转换任务 × 注入语义错误 × 对照），验证"程序校验查形状 vs 模型自查查语义"的分工与边界。此处只留工业级结论，实验细节见该学习项目。

1. **程序校验与模型自查分工互补**：程序校验（jsonschema）查**形状**（字段 / 类型 / 值域），确定性、便宜；模型自查查**语义**（配对正确、数值精确），概率性、多一次 LLM 调用。程序校验查不了"配对是否正确"——4 项都在、类型全对、结果填错位置照样放行，自查能补。
2. **自查范围 = 提示词清单（Scope 盲区）**：审查指令只查列出的维度，清单外的必漏——实验实证 `answer` 与 `results` 自相矛盾（清单外），自查直接放行。设计自查提示词要**穷举要查的维度**（字段间一致性、单位方向、证据支撑…），漏一个就是漏报温床。这也是独立 Critic Agent（不同上下文 / 视角）存在的理由——同模型自查被指令范围锁死。
3. **自查是概率性的，精度取决于错误类型**：客观对照类（数值偏差 / 数量 / 重复）抓得准但报告索引可能错位；语义配对类（互换 / 单位错位）会多报（false positive）。多报无害（修正兜底），**漏报危险**。
4. **能规则化的交给规则**：jsonschema 能拦截的（`maxItems` / `minItems` / `pattern` / `enum`）尽量规则化，不要押给概率性自查。
5. **修正环节用地面真值兜底**：Refine 基于完整上下文（工具记录 / 真实结果）重写，不盲目采纳 issues——自查的误差被修正兜底。**自查不完美，但错了也无害**，前提是修正有真实依据。

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
