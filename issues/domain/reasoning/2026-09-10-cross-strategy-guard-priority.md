# 跨策略执行护栏优先级与调用后复查不一致（REASON-016）

> **状态**：✅ 已修复 ｜ **优先级**：P1 ｜ **来源**：上下文预算跨策略闭环 Slice 2 审核 ｜ **涉及模块**：`app/domain/reasoning/_common.py`、`react.py`、`reflection.py`、`planner.py`

## 问题描述

三个推理策略共同检查用户取消、总执行期限和累计成本，但共享函数返回两个字符串，调用方只能通过空值组合推断终止类型。上下文超限又在 ReAct 中单独处理，没有一份可直接测试的四类优先级契约。

检查位置也不对称：ReAct 只在调用后检查成本，调用方基线已经超限时仍会多发首笔请求；Reflection 在自查返回后不复查，期间发生取消或成本超限仍会继续修正；Planner 在子 ReAct 返回后先检查取消，导致已经生成的 usage、迭代和当前步骤成果未被吸收。

ReAct 的终止成果选择还把当前轮未执行的 `tool_calls` 当作用户可见成果。当前轮只有工具意图时，它会覆盖上一轮正文，但最终 outcome 不会执行或呈现这些工具调用。

## 根因

- 共享守卫只返回展示字符串，缺少类型和值的稳定契约，无法集中表达优先级。
- 各策略自行决定检查时点，没有统一遵守“调用前准入；成功后先接管结果和 usage，再复查”的生命周期规则。
- `current_result` 的可见性判断混合了模型意图和已经可交付的内容。

## 修复方案

- 在 `_common.py` 定义不可变 `GuardResult`，复用 `AgentErrorKind`，不新增平行枚举。
- `evaluate_guard` 使用 monotonic 绝对 deadline，固定优先级：`CANCELLED > TIMEOUT > COST_EXCEEDED > CONTEXT_EXCEEDED`。LLM Facade 已判定的取消/deadline 通过显式标记进入同一判定，上下文异常通过 `context_error` 进入最后一级。
- ReAct、Reflection、Planner 均在付费调用前准入，并在成功结果与 usage 归账后复查。命中后禁止工具副作用、修正、重规划、汇总或下一轮请求。
- Planner 子 ReAct 返回后先吸收 outcome、usage、迭代并记录当前步骤，再按共享优先级终止。
- Planner 汇总成功后若护栏命中，保留已经返回的 structured、摘要与 usage，再以降级终态结束。
- ReAct 仅把当前轮 content/reasoning 视为可见成果；只有未执行 tool_calls 时沿用上一轮可见结果。

## 决策取舍

类型定义保留在 Domain reasoning 的 `_common.py`，因为它表达策略执行语义，并只依赖 Domain ports 与 shared 类型。Integration 的最终请求预算闸仍独立负责模型窗口准入；Domain 只消费其 `ContextWindowExceededError`，没有把两类上下文管理合并。

没有修改 `CostLimiterPort` 或 `LLMGateway` 契约。共享函数不保存累计状态，usage 口径仍由各策略负责，避免引入跨运行可变状态。

## 验证

- `tests/unit/test_reasoning_common.py`：不可变结果、deadline 等号边界、四类优先级与短路成本检查。
- `tests/unit/test_react_strategy.py`：基线成本超限时零 LLM 调用、流式 cancel/cost 同时命中时取消优先且 usage 不丢；上一轮有可见正文而当前轮只有未执行 tool_calls 时，调用后成本终止与工具执行期超时均保留上一轮正文。
- `tests/unit/test_react_strategy_nonstream.py`：非流式成功返回后 cancel/cost 组合与流式语义一致。
- `tests/unit/test_reflection.py`：自查后取消或成本超限时不再发起修正，采用初稿并只产生一个最终 done。
- `tests/unit/test_planner.py`：子 ReAct 累计成本越界后保留当前失败步骤和 usage，不再 replan/summarize；汇总成功后取消仍保留 structured 与 usage。

## 教训沉淀

守卫判断必须返回领域类型，展示文案不能承担控制流协议。异步调用返回成功值时，先接管结果和计费用量，再按固定优先级终止；否则“及时停止”会演变成结果或成本丢失。
