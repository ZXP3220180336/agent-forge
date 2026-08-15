# 提示词模块说明文档

> **更新日期**：2026-08-03
> **模块**：`app/domain/prompts/`
> **实现状态**：✅ 已实现（planning 模板为预留 draft）
> **架构定位**：核心层的提示词管理，为 Agent 提供系统/工具/规划等场景的提示词模板

---

## 📋 目录

- [模块概述](#模块概述)
- [实现状态总览](#实现状态总览)
- [核心组件详解](#核心组件详解)
- [使用示例](#使用示例)
- [相关文档](#相关文档)

---

## 模块概述

提示词模块负责组装不同场景下的完整提示词，是 Agent 与 LLM 之间的**指令层**：

- **系统提示词**：定义 Agent 的能力、工作方式与原则
- **工具格式提示词**：告诉 LLM 工具调用的格式规范
- **规划提示词**：Plan-then-Execute 策略的任务分解（预留）

```
Agent 层
    ↓
PromptManager.build_system_prompt()
    ↓
系统提示词 + 工具格式说明 → messages[0]（system 角色）
```

---

## 实现状态总览

| 文件 | 状态 | 内容 |
| --- | --- | --- |
| `__init__.py`（8行） | ✅ | 子包导出 |
| `base.py`（19行） | ✅ | `PromptTemplate` 基类 |
| `manager.py`（22行） | ✅ | `PromptManager` 管理器 |
| `templates/system.py`（25行） | ✅ | `SYSTEM_PROMPT` 系统提示词 |
| `templates/tools.py`（12行） | ✅ | `TOOL_FORMAT_PROMPT` 工具格式提示词 |
| `templates/planning.py`（18行） | 🔶 | `PLANNING_PROMPT` 规划提示词（draft，Phase 2 预留） |

---

## 核心组件详解

### `PromptManager`

```python
class PromptManager:
    @staticmethod
    def build_system_prompt(tool_descriptions: str = "") -> str:
        """构建系统提示词：SYSTEM_PROMPT + 工具格式说明（可选）"""
```

- `build_system_prompt(tools_desc)`：组装 `SYSTEM_PROMPT`，若传入工具描述则追加 `TOOL_FORMAT_PROMPT.format(tools=tools_desc)`

### `PromptTemplate` — 模板基类

```python
class PromptTemplate:
    def __init__(self, template: str): ...
    def format(self, **kwargs) -> str: ...  # 变量填充
    @property
    def raw(self) -> str: ...               # 原始模板
```

### 系统提示词（SYSTEM_PROMPT）

定义了 Agent 的：
- **核心能力**：调用工具获取信息、分析需求、综合结果
- **工作方式**：按需调用搜索/文件/代码/网页工具
- **原则**：不确定先搜索、结果通俗解释、失败尝试其他方式、不编造信息

### 工具格式提示词（TOOL_FORMAT_PROMPT）

用于向 LLM 说明工具的格式规范，通过 `format(tools=...)` 注入工具描述列表。

### 规划提示词（PLANNING_PROMPT，预留）

Phase 2 预留：Plan-then-Execute 策略的任务分解模板（将需求拆解为步骤，标注工具/参数/输出/依赖）。

---

## 使用示例

```python
from app.core.prompts.manager import PromptManager

pm = PromptManager()

# 不带工具描述（简单问答）
system_prompt = pm.build_system_prompt()

# 带工具描述（Agent 需要调用工具）
tools_desc = "\n".join(f"- {t.name}: {t.description}" for t in tool_service.list_tools())
system_prompt = pm.build_system_prompt(tools_desc)

messages = [{"role": "system", "content": system_prompt}]
```

---

## 相关文档

- [核心层说明](../../domain_doc/README.md)
- [Agent 模块详解](../../domain_doc/agent_doc/agent.md)
- [架构设计](../../architecture.md)
