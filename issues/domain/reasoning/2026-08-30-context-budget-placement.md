# REASON-001 上下文预算仅工具路径生效：非工具重试路径漏裁

> **状态**：✅ 已修复（2026-08-30）
> **优先级**：P1（核心护栏 #10 功能缺陷，非崩溃）
> **来源**：2026-08-30 代码审查发现（上下文预算放置位置与语义错位）
> **涉及模块**：`app/domain/reasoning/react.py`（`execute` 主循环 / `_handle_tool_calls`）
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md) · [ADR context-budget](../../../adr/domain/reasoning/2026-08-28-context-budget.md)

---

## 问题描述

### 现象

上下文预算（`ContextBudgetPort.trim_messages`）只在 `_handle_tool_calls` 末尾调用，**仅覆盖「工具调用 → 回喂 → 下一轮」这一条路径**。以下三条**非工具调用但继续下一轮**的路径完全漏裁，上下文随轮次无限增长、预算护栏失效：

1. **LLM 调用失败 → handler CONTINUE 重试**（`_finalize_llm_failed` CONTINUE 后 `continue`）
2. **final_answer 参数校验失败 → CONTINUE 回喂重试**（`_handle_final_answer` CONTINUE 后 `continue`）
3. **空输出 → CONTINUE 重试**（`_handle_empty_output` 默认 CONTINUE，落入下一轮）

### 影响

- 这些路径每轮 append 一条 assistant 消息但不裁剪 → 上下文线性膨胀，`max_context_rounds` / `max_context_tokens` 形同虚设；
- 长任务（持续空输出重试 / 持续 LLM 失败重试）token 最终超模型上下文窗口 → 400 或费用膨胀，护栏本应防止的正是此场景。

### 根因

预算语义是「**每次 LLM 调用前**作为 gatekeeper 裁剪」（横切护栏，与工具是否被调用无关），但实现时按「**工具调用后**」这一局部时机放置——放置点锚定了错误的分支路径，而非其真正约束的调用点（LLM 调用）。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| LangGraph（checkpointer / trimmer） | 消息截断（truncate）挂在**模型节点之前**的 transform 链上，任何进入模型调用的状态都过同一道裁剪，与分支无关 |
| OpenAI Agent SDK / Claude 多轮循环 | 上下文窗口管理是**每次模型调用前的门卫**，工具调用与否不改变裁剪时机 |
| 本项目 context_manager 注释 | 「模型下次调用前作为 gatekeeper 裁剪」——注释语义本即循环顶部，实现位置与之错位 |

**核心**：护栏的放置点应锚定「其约束的调用点」（LLM 调用），而非「某条分支路径」（工具调用后）。

---

## 修复方案（含决策取舍）

**决策**：预算块从 `_handle_tool_calls` 移到 `execute()` 主循环**顶部、LLM 推理前**（第 0 步），所有继续路径共用；`_handle_tool_calls` 移除预算逻辑与 `max_context_rounds` / `max_context_tokens` 两个参数。

**取舍理由**：

1. **覆盖完整**：工具回喂 / LLM 失败重试 / final_answer 回喂重试 / 空输出重试，下一次 LLM 调用前均裁剪——一处放置、全路径生效，无重复代码；
2. **工具路径行为不变**：原实现时机为「工具结果后、下一轮 LLM 前」，新实现为「下一轮 LLM 前」，中间无其他消息追加——等价裁剪，既有工具路径测试原样通过；
3. **不四处埋预算**：若在各 CONTINUE 分支（`_finalize_llm_failed` / `_handle_final_answer` / `_handle_empty_output`）各加一次调用，则裁剪点散落 4 处、易漏易漂移——集中到循环顶部唯一。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/reasoning/react.py` | 预算块移至主循环顶部（第 0 步，注释说明覆盖所有继续路径）；`_handle_tool_calls` 移除预算与 `max_context_rounds` / `max_context_tokens` 参数（签名、调用处、docstring 同步）；`execute()` docstring 循环流程补第 0 步 | `tests/unit/test_react_strategy.py` 新增 `test_react_context_budget_trims_on_no_tool_retry`（空输出重试路径，max_context_rounds=2） |
| `docs/domain_doc/reasoning_doc/react.md` | 上下文预算节 / 行为边界 #9 / `_handle_tool_calls` 行 / 测试节（30→31 用例）同步 | — |

---

## 验证

- **测试驱动**：先写 `test_react_context_budget_trims_on_no_tool_retry`，修复前 `assistant_count == 6`（无裁剪，断言 `<= 3` 失败）→ 修复后 `== 3`（每轮顶部 trim，末轮 append 未再 trim +1）；
- `tests/unit/test_react_strategy.py` 31 passed（30 既有 + 1 新增）；
- `uv run pytest` 全量通过；`uv run python -m scripts.verify_alignment` 通过。

---

## 教训沉淀

- **横切护栏的放置点锚定「其约束的调用点」，而非「某条分支路径」**：预算/限流/裁剪类能力，审查时列出所有通往约束调用点（这里是 LLM 调用）的路径，逐条确认护栏覆盖，而非只看实现所在的那条主路径；
- **护栏类能力加"覆盖完整性"断言**：预算守卫的测试不应只测「有工具时生效」，还要测「无工具但继续循环的路径」（重试 / 回喂）同样生效——这是护栏失效最常见的隐性缺口。
