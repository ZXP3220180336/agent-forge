# ReAct 将 LLM 内部 TimeoutError 误判为总执行超时（REASON-014）

> **状态**：✅ 已修复 ｜ **优先级**：P1 ｜ **来源**：LLM-046 跨层审查 ｜ **涉及模块**：`app/domain/reasoning/react.py`、`app/integration/llm/streaming_rectifier.py`

## 问题描述

LLM-046 修复后，半流续接已经读到 EOF 时，结算或成功日志抛出的 `TimeoutError` 会原样穿透 `StreamingRectifier` 与 `LLMService`。ReAct 原先用单独的 `except TimeoutError` 无条件映射 `TIMEOUT`，因此把组件收尾失败误报为 `max_execution_time` 耗尽。

错误分类还掩盖了第二个缺口：异常发生在本轮正常归账前，若改走原 UNKNOWN 分支但继续只使用 `last_result`，续接已生成的内容、reasoning 和 usage 仍会丢失。

## 根因

内置 `TimeoutError` 只表示异常类型，不能说明异常来源。ReAct 同时存在两个可能抛出该类型的来源：外层 `asyncio.timeout_at` 的硬墙，以及 LLM/日志/结算等内部组件。旧实现没有保留 timeout 上下文对象，无法确认当前异常是否由本次硬墙实际到期产生。

UNKNOWN 分支沿用“上一完成轮”状态，也没有接管正在填充但尚未归账的 `current_result`。

## 工业级参照

Python `asyncio.Timeout.expired()` 专门用于检查 timeout 上下文是否实际到期；`asyncio.timeout()` / `timeout_at()` 只会在其上下文退出时把自身触发的取消转换为 `TimeoutError`（[Python Coroutines and Tasks](https://docs.python.org/3/library/asyncio-task.html#timeouts)）。因此异常类型负责粗分组，timeout 对象的状态负责来源确认。

## 修复方案

- 保存 `asyncio.timeout_at()` 返回的 `Timeout` 对象，并在退出上下文后的 `except TimeoutError` 中检查 `expired()`。
- 仅当该对象确实到期时走 `_finalize_timeout`；普通 `TimeoutError` 走 `_finalize_unknown`。
- 普通 `TimeoutError` 与其他 UNKNOWN 异常均优先选择有可见进度的 `current_result`，并合并尚未正常归账的 usage；当前轮为空时回退 `last_result`。
- 保留原有生成器关闭判别；外部 `task.cancel()` 继续传播 `CancelledError`，异 task `aclose()` 不生成伪终态。

没有使用 `loop.time() >= hard_timeout_at` 推断来源：期限已过不等于 timeout 回调已实际取消当前 task，该比较会在事件循环调度边界误判内部异常。

## 实施记录

`ReActStrategy.execute` 绑定 `hard_timeout_scope`，以 `hard_timeout_scope.expired()` 区分硬超时和内部同名异常。TimeoutError 的 UNKNOWN 路径与通用异常路径都通过 `_terminal_result`、`_unaccounted_usage` 保留当前轮成果和成本口径。

Integration 的异常契约不变：LLM-046 的完成态守卫继续原样上抛收尾异常，`LLMService` 只翻译执行终止信号，不把普通 `TimeoutError` 冒充 deadline。

## 验证

- `tests/unit/test_react_strategy.py`：有硬期限与 `None` 两种配置下，内部 `TimeoutError` 均归 UNKNOWN，当前轮 content/reasoning/usage 保留；真实硬墙仍归 TIMEOUT；普通 UNKNOWN、外部 task cancel、异 task `aclose()` 均有回归护栏。
- `tests/unit/test_llm_request_budget.py`：真实 `LLMService.async_generate` 走“首流中断 → 一次续接 → EOF → 成功日志 TimeoutError”，断言 SDK create 恰为 2 次、ReAct 归 UNKNOWN，并保留续接内容与 usage。

## 教训沉淀

同一种异常类型跨越层次后可能代表不同来源。控制流分类不能只看类名；创建控制范围的一层必须保留可验证的来源状态，并在异常边界使用该状态判定。异常前已经产生的流式结果仍属于当前执行，分类修正必须同时完成内容和 usage 的所有权收尾。
