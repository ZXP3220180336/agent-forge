# ReAct 内部 deadline 与外层 timeout 同刻竞争（REASON-013）

> **状态**：✅ 已修复 ｜ **优先级**：P1 ｜ **来源**：LLM-044 资源收尾审查 ｜ **涉及模块**：`app/domain/reasoning/react.py`

## 问题描述

ReAct 外层硬超时与传入 Integration LLM 链的绝对 deadline 使用同一到期时刻。两者几乎同时触发时，外层对当前 task 的硬取消可以打断内部 deadline 已开始的 stream close、reservation settle 和日志收尾。

## 根因

业务终止与 task 取消没有时序层次。类型化 deadline 需要协作式收尾；两者同刻会让外层 timeout 取消正在运行的清理。

## 工业级参照

Python `asyncio.timeout` 通过取消当前 task 实现超时，并在上下文外转为 `TimeoutError`；被取消协程应用 `try/finally` 完成清理（[Coroutines and Tasks](https://docs.python.org/3/library/asyncio-task.html)）。它定义取消触发时刻，不保证同步阻塞或吞取消代码立即返回。本项目因此将协作式内部 deadline 放在外层 task 取消之前。

## 修复方案

- `max_execution_time` 表示 ReAct 业务循环的 timeout 取消触发点。
- 内部 LLM deadline 设为该触发点前 `min(1s, 总时长 × 10%)`，窗口仅供 Integration 终止清理使用。
- 外层使用事件循环时钟计算 `timeout_at`；Integration 的 deadline 继续按 `time.monotonic()` 契约计算，避免混用时钟域。
- 不识别内部 deadline、但配合 task 取消的 LLM 仍在原 timeout 时刻收到取消。
- `_finalize_guard_result(TIMEOUT)`、错误 handler 与 done 事件生成位于 timeout scope 外，是不再发起 LLM/工具副作用的领域尾部；本实现不对同步阻塞或吞取消扩展点承诺绝对返回时限。

## 决策取舍

没有将外层 timeout 触发点延后一个 grace，因为 ReAct 循环还包含工具调用。方案以最多 1 秒的 LLM 可用时间换取资源收尾窗口；极短任务只预留 10%，不会把业务窗口压缩为零。

## 实施记录

`ReActStrategy.execute` 新增私有最大清理窗口与比例常量，分别计算 Integration deadline 和外层 `hard_timeout_at`。

## 验证

`tests/unit/test_react_strategy.py` 覆盖：内部 deadline 后的延迟清理能在预留窗口内完成；忽略内部 deadline、但配合 task 取消的 LLM 在 timeout 触发点停止。`tests/unit/test_llm_request_budget.py` 以真实 `LLMService`/`StreamingRectifier` 边界覆盖 `_drain → close → settle`：窗口内按 close、settle、log 顺序完成；close 超过窗口时外层取消生效，`finally` 仍保守 `settle(None)`，且不产生整流、续接或 fallback 新请求。

## 教训沉淀

协作式 deadline 与 timeout task 取消不应共用同一时刻；前者需要有界收尾窗口。描述时间契约时必须区分“到期发起取消”和“调用绝对返回”，并明确 timeout scope 外的无副作用尾部。
