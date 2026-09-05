# 提示词模块对外接口文档

> **对应代码**：`app/domain/prompts/`
> **更新日期**：2026-09-05
> **文档定位**：提示词模块对外接口文档——`PromptManager` 接口契约 + 内部模板导航；服务对象为领域层 Agent 编排（ReActAgent / PlannerAgent / ReflectionAgent）
> **实现状态**：✅ 已实现（system / tools / reflection / planning 模板 + manager builder 全覆盖；planning 三模板随 Planner 落地）

---

## 📋 目录

- [提示词模块对外接口文档](#提示词模块对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心功能](#核心功能)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [依赖关系](#依赖关系)
  - [对外接口](#对外接口)
    - [PromptManager 方法表](#promptmanager-方法表)
    - [PromptTemplate 方法表](#prompttemplate-方法表)
    - [最小调用示例](#最小调用示例)
  - [内部实现组织](#内部实现组织)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心功能

提示词模块是领域层的**指令层**，负责组装不同场景下的完整提示词，是 Agent 与 LLM 之间的指令接口：

- **系统提示词**：定义 Agent 的能力、工作方式与原则（`SYSTEM_PROMPT`）
- **工具格式提示词**：告诉 LLM 工具调用的格式规范（`TOOL_FORMAT_PROMPT`）
- **自查 / 修正提示词**：Reflection 自查与修正指令（`CRITIQUE_PROMPT` / `REFINE_PROMPT`）
- **规划提示词**：Planner 三阶段指令——规划 / 重规划 / 汇总（`PLANNING_PROMPT` / `REPLAN_PROMPT` / `SUMMARIZE_PROMPT`）

### 模块结构

```text
app/domain/prompts/
├── __init__.py          # 子包导出
├── base.py              # PromptTemplate 模板基类
├── manager.py           # PromptManager 管理器（对外入口）
└── templates/           # 提示词模板
    ├── system.py        # SYSTEM_PROMPT 系统提示词
    ├── tools.py         # TOOL_FORMAT_PROMPT 工具格式提示词
    ├── reflection.py    # CRITIQUE_PROMPT / REFINE_PROMPT 自查与修正提示词
    └── planning.py      # PLANNING_PROMPT / REPLAN_PROMPT / SUMMARIZE_PROMPT 规划提示词
```

### 设计原则

1. **模板与组装分离**：`templates/` 只存模板常量，`PromptManager` 负责组装（系统提示词 + 可选工具格式说明）
2. **单一事实源**：提示词内容只在本模块定义，Agent 编排经 `PromptManager` 获取，不散落复制
3. **变量插值**：`PromptTemplate` 提供 `format()` 变量填充（如工具描述列表注入）

### 依赖关系

```text
Agent 编排（ReActAgent / PlannerAgent / ReflectionAgent）
        ▼ 调用 build_*
PromptManager（本模块，对外入口）
    ├── templates/system.py（SYSTEM_PROMPT）
    ├── templates/tools.py（TOOL_FORMAT_PROMPT）
    ├── templates/reflection.py（CRITIQUE_PROMPT / REFINE_PROMPT）
    └── templates/planning.py（PLANNING_PROMPT / REPLAN_PROMPT / SUMMARIZE_PROMPT）
```

`prompts/` 只依赖标准库，被 agent/ 编排调用；不依赖任何外部框架。

---

## 对外接口

### PromptManager 方法表

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `build_system_prompt(tool_descriptions: str = "")` | 同步静态 | 构建系统提示词：`SYSTEM_PROMPT` + 可选 `TOOL_FORMAT_PROMPT.format(tools=...)` |
| `build_reflection_critique_prompt(evidence, draft)` | 同步静态 | 构建 Reflection 自查指令：证据链 + 初稿 |
| `build_reflection_refine_prompt(evidence, draft, issues)` | 同步静态 | 构建 Reflection 修正指令：证据链 + 初稿 + 审查意见 |
| `build_planning_prompt(user_input, tool_descriptions)` | 同步静态 | 构建 Planner 规划指令：用户目标 + 工具目录（introspection 文本，不入 tools） |
| `build_planning_replan_prompt(goal, tool_descriptions, executed, failed_step, error)` | 同步静态 | 构建 Planner 重规划指令：已完成步骤摘要 + 失败步骤 + 原因 |
| `build_planning_summarize_prompt(goal, executed)` | 同步静态 | 构建 Planner 汇总指令：各步骤结果 → 证据链报告 |

序列化辅助：`_serialize_evidence`（reflection 证据链，剔除 final_answer 终止条目）+ `_serialize_step_results`（planner 步骤执行记录，含成败 + 产出摘要 + 工具引用）——单条截断 500 字符 / 总量截断 4000 字符防上下文膨胀。

### PromptTemplate 方法表

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `__init__(template: str)` | 构造 | 绑定原始模板 |
| `format(**kwargs) -> str` | 同步 | 变量填充（如 `tools`） |
| `raw` | 属性 | 返回原始模板 |

### 最小调用示例

```python
from app.domain.prompts.manager import PromptManager

pm = PromptManager()

# 不带工具描述（简单问答）
system_prompt = pm.build_system_prompt()

# 带工具描述（Agent 需要调用工具）
tools_desc = "\n".join(f"- {t.name}: {t.description}" for t in tool_service.list_tools())
system_prompt = pm.build_system_prompt(tools_desc)

messages = [{"role": "system", "content": system_prompt}]
```

---

## 内部实现组织

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| `SYSTEM_PROMPT` | templates/system.py | 系统提示词：核心能力 / 工作方式 / 原则 | ✅ |
| `TOOL_FORMAT_PROMPT` | templates/tools.py | 工具格式说明（`format(tools=...)` 注入工具列表 + 截断提示） | ✅ |
| `CRITIQUE_PROMPT` / `REFINE_PROMPT` | templates/reflection.py | Reflection 自查 / 修正提示词（见 [reflection.md](../reasoning_doc/reflection.md)） | ✅ |
| `PLANNING_PROMPT` / `REPLAN_PROMPT` / `SUMMARIZE_PROMPT` | templates/planning.py | Planner 规划 / 重规划 / 汇总提示词（见 [planner.md](../reasoning_doc/planner.md)） | ✅ |
| `PromptManager`（builder + 序列化） | manager.py | 组装入口 + 证据/步骤序列化（方法见「PromptManager 方法表」） | ✅ |

---

## 相关文档

- [领域层说明](../README.md)
- [Agent 模块对外接口文档](../agent_doc/agent.md)
- [推理策略模块](../reasoning_doc/reasoning.md)（ReAct / Reflection / Planner 策略消费本模块提示词）
- [架构设计](../../architecture.md)
