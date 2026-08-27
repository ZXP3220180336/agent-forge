# 提示词模块对外接口文档

> **对应代码**：`app/domain/prompts/`
> **更新日期**：2026-08-27
> **文档定位**：提示词模块对外接口文档——`PromptManager` 接口契约 + 内部模板导航；服务对象为领域层 Agent 编排（ReActAgent / PlannerAgent / ReflectionAgent）
> **实现状态**：🔶 已实现（组件就绪，测试待补；planning 模板为预留 draft）

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
- **规划提示词**：Plan-then-Execute 策略的任务分解（`PLANNING_PROMPT`，draft）

### 模块结构

```text
app/domain/prompts/
├── __init__.py          # 子包导出
├── base.py              # PromptTemplate 模板基类
├── manager.py           # PromptManager 管理器（对外入口）
└── templates/           # 提示词模板
    ├── system.py        # SYSTEM_PROMPT 系统提示词
    ├── tools.py         # TOOL_FORMAT_PROMPT 工具格式提示词
    └── planning.py      # PLANNING_PROMPT 规划提示词（draft）
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
    └── templates/planning.py（PLANNING_PROMPT）
```

`prompts/` 只依赖标准库，被 agent/ 编排调用；不依赖任何外部框架。

---

## 对外接口

### PromptManager 方法表

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `build_system_prompt(tool_descriptions: str = "")` | 同步静态 | 构建系统提示词：`SYSTEM_PROMPT` + 可选 `TOOL_FORMAT_PROMPT.format(tools=...)` |

> 预留能力：planning / reflection 场景的 builder 将随对应策略落地（`build_planning_prompt` 等），见内部实现组织。

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
| `SYSTEM_PROMPT` | templates/system.py | 系统提示词：核心能力 / 工作方式 / 原则 | 🔶 |
| `TOOL_FORMAT_PROMPT` | templates/tools.py | 工具格式说明（`format(tools=...)` 注入工具列表 + 截断提示） | 🔶 |
| `PLANNING_PROMPT` | templates/planning.py | 规划提示词（任务拆解步骤，draft，随 PlannerAgent 落地） | 🔶 draft |

---

## 相关文档

- [领域层说明](../README.md)
- [Agent 模块对外接口文档](../agent_doc/agent.md)
- [架构设计](../../architecture.md)
