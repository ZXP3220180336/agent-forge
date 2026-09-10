# ReAct deadline 终止丢失当前轮部分成果（REASON-012）

> **状态**：✅ 已修复 ｜ **优先级**：P1 ｜ **来源**：LLM-044 终止链审查 ｜ **涉及模块**：`app/domain/reasoning/react.py`

## 问题描述

流式 LLM 调用在 `async_generate` 返回前会逐步写入本轮 `StreamResult`。当内部 deadline 在本轮中途触发时，ReAct 的异常分支只用上一完成轮 `last_result` 组装 outcome，当前轮已生成的 content/reasoning/tool calls 与可得 usage 会丢失。

该缺口会使良率 RCA 报告在时间耗尽时丢掉最新证据推理，同时低估已发生的 token 消耗。

## 根因

`last_result` 只在一轮 LLM 调用正常返回后赋值；正在填充的 `stream_result` 没有跨越 `await` 边界的显式所有权标记。deadline 是异常出口，因此终止分支无法选择当前轮成果。

## 工业级参照

Python `asyncio` 要求被取消协程用 `try/finally` 执行可靠收尾，这意味着跨取消边界的部分状态必须由上层显式持有，不能只在正常返回点交接（[Coroutines and Tasks](https://docs.python.org/3/library/asyncio-task.html)）。

## 修复方案

- 每轮创建 `StreamResult` 后立即赋给 `current_result`，正常完成用量归账后清空。
- 终止时当前轮已有 content、reasoning 或 tool calls 则优先使用；当前轮无可见进度时保留上一完成轮，避免空结果覆盖已有成果。
- usage 以类型化异常携带值优先，否则使用 `current_result.usage`，两者不叠加，防止双计。

## 实施记录

`ReActStrategy.execute` 引入 `current_result`、`_terminal_result` 和 `_unaccounted_usage`，并在内部 deadline、优雅取消及外层硬超时出口统一使用。非流式轮末取消也在收尾前先归账已完成响应的 usage。

## 验证

`tests/unit/test_react_strategy.py` 覆盖当前轮部分成果、当前轮为空时回退上一轮、外层硬超时保留部分成果。`tests/unit/test_react_strategy_nonstream.py` 覆盖非流式轮末取消 usage 保留。

## 教训沉淀

流式结果的所有权必须在可中断 `await` 之前交给收尾层；“上一完成轮”与“当前部分轮”是两类状态，不能用同一变量表达。
