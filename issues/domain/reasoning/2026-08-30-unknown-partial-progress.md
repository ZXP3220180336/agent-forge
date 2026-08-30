# REASON-002 未捕获异常（UNKNOWN）路径不保留部分进度，与其余终结护栏不一致

> **状态**：✅ 已修复（2026-08-30）
> **优先级**：P1（异常路径证据链丢失，与 TIMEOUT / COST_EXCEEDED / STALLED 的部分进度保留语义不一致）
> **来源**：2026-08-30 代码审查发现（Agent 运行异常处理全面性评审）
> **涉及模块**：`app/domain/reasoning/react.py`（`execute` 主循环 / `_finalize_unknown`）
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md)

---

## 问题描述

### 现象

ReAct 主循环只有 `except TimeoutError` 捕获超时；**其他未捕获异常**（如 `context_budget.trim_messages` / dispatch handler 等第三方实现抛异常）直接传播到 `BaseAgent.run` 的 `except Exception` → `UNKNOWN` 分发 → `outcome` 未设置 → **已完成的工具调用记录（证据链）与部分内容全部丢失**（`agent.result` 为 None，error 仅「ReAct 策略未产出结果」）。

### 影响

- 良率排查中 Agent 已完成多轮工具调查后遇到一个意外异常，之前的全部调查轨迹（tool_calls / 部分内容）丢失——产品主链路的证据链断裂；
- 与 `TIMEOUT` / `COST_EXCEEDED` / `STALLED` 的「用 `last_result` 保留部分进度」模式**不一致**：同一套终结护栏，唯独 UNKNOWN 这条路径例外。

### 根因

主循环 `try` 块只 `except TimeoutError`，未捕获的 `Exception` 逃逸到 BaseAgent 层——UNKNOWN 处理点不在策略层，策略层持有的 `last_result` / `_tool_call_records` 无法带入 outcome。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| LangChain AgentExecutor | 异常时 `intermediate_steps`（已执行工具轨迹）保留在 AgentFinish 输出，部分进度不丢 |
| OpenAI Agent SDK / SMOLagents | 异常 / 中断时 Result 携带已完成的步骤与工具输出 |
| 本项目 `_finalize_timeout` / `_finalize_cost_exceeded` / `_finalize_stalled` | 均用 `last_result` 组装降级 outcome 保留部分进度 + `_tool_call_records` 进证据链（内部已确立的先例） |

**核心**：终结性兜底路径应统一保留部分进度，异常路径不应是例外。

---

## 修复方案（含决策取舍）

**决策**：主循环异常结构对齐既有模式，新增两个分支：

1. **`except AgentRunError: raise`**（置于最前）——RAISE 决策的领域错误不吞，传播到 `BaseAgent.run` 统一 re-raise，**不被下方 `except Exception` 兜底转 UNKNOWN**（否则用户注册 handler→RAISE 会被吞）。
2. **`except Exception as e`** → 新 `_finalize_unknown(last_result, iteration, total_usage, e)`：复用 `_finalize_terminal(UNKNOWN, ...)`（dispatch 一次、CONTINUE 忽略、默认 STOP），用 `last_result` 组装 outcome（保留证据链 + 部分内容），error 记录「Agent 运行异常: <截断文本>」。**关闭判别对齐 `except TimeoutError`**（生成器被 finalizer/aclose 关闭时干净停止）。

**取舍理由**：

1. **RAISE 语义统一**：UNKNOWN 走主循环后，其 RAISE 从 BaseAgent 的「re-raise 原始异常」变为统一的「抛 `AgentRunError(kind=UNKNOWN)`」——与其余 11 类 kind 完全一致（handler→RAISE 一律 AgentRunError 携带 kind）；原始异常文本保留在 message（截断到 200）。
2. **`asyncio.CancelledError` / `GeneratorExit` 是 `BaseException`**，不被 `except Exception` 捕获 → CANCELLED / 生成器关闭语义不变。
3. **不引入新 kind**：UNKNOWN 已是兜底 kind，仅扩展其触发点（主循环兜底 + BaseAgent 逃逸兜底并存）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/reasoning/react.py` | 主循环加 `except AgentRunError: raise` + `except Exception as e` → `_finalize_unknown`；新方法 `_finalize_unknown`（复用 `_finalize_terminal`，用 last_result 组装保留证据链）；`_dispatch` docstring 10→11 处 | `tests/unit/test_react_strategy.py` 新增 3 例：中途异常保留证据链 / UNKNOWN→RAISE 抛 AgentRunError / CONTINUE 忽略 |
| `tests/unit/test_agent.py` | `test_unknown_handler_raise_propagates` 期望修正：UNKNOWN RAISE 现在抛 `AgentRunError(kind=UNKNOWN)`（语义统一），原期望 re-raise `RuntimeError` 已过时 | — |
| `docs/domain_doc/reasoning_doc/react.md` | 错误分发表加 UNKNOWN 行 / kind 计数 11 类 / 执行流程加异常兜底步 / 边界 #17 / 测试 54→57 用例 / `_dispatch` 10→11 处 | — |

---

## 验证

- **测试驱动**：`test_react_unknown_exception_keeps_partial_progress` 先写——第 1 轮工具执行成功、第 2 轮 LLM 调用抛异常 → 修复后 `outcome.tool_calls == 1`（证据链保留），修复前异常传播 outcome 为 None（测试失败）；
- 修复过程中发现 **6 个既有 handler→RAISE 测试回归**：`except Exception` 吞掉了 RAISE 决策的 `AgentRunError` → 加 `except AgentRunError: raise` 前置分支后恢复；
- `tests/unit/test_react_strategy.py` 57 passed；`uv run pytest` 全量 676 passed；`verify_alignment` 通过。

---

## 教训沉淀

- **终结兜底路径统一「部分进度保留」**：评审终结性护栏（TIMEOUT / COST / STALLED / MAX_TURNS / UNKNOWN）时，逐条确认是否用 `last_result` 保留证据链——「兜底」应兜住已有成果，而非清空重来；
- **主循环 `except Exception` 必须前置 `except AgentRunError`**：通用异常兜底会吞掉 RAISE 决策的领域错误（`_dispatch` 抛出的 `AgentRunError`），导致 handler→RAISE 语义被改写为 UNKNOWN——兜底捕获先排除领域错误；
- **RAISE 语义全局一致**：所有 kind 的 handler→RAISE 统一抛 `AgentRunError`（携带 kind），不因处理点不同（策略层 vs BaseAgent 层）而 re-raise 原始异常——调用方按 kind 差异化处理，行为可预期。
