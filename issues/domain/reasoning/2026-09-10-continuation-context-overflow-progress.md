# 续接上下文超限丢失当前轮成果与用量（REASON-015）

> **状态**：✅ 已修复 ｜ **优先级**：P1 ｜ **来源**：第一阶段 P1/P2 验收复核 ｜ **涉及模块**：`app/domain/reasoning/react.py`、`app/integration/llm/streaming_rectifier.py`

## 问题描述

流式请求已经产出部分正文后发生可恢复中断，整流器会用当前正文作为前缀发起续接。前缀扩大使续接请求命中 `ContextWindowExceededError` 时，ReAct 的上下文超限出口只使用上一完成轮，导致本轮已经发送给客户端的内容和已取得 usage 从最终 outcome 中丢失。

同时，续接链在调用 `continue_fn` 前清除了死流的 `usage`、`finish_reason` 和 `refusal`。预算闸在新 SDK 请求发出前拒绝时，新流所有权尚未建立，旧请求的实际 usage 却已被提前丢弃。

## 根因

终止结果选择只在 deadline/cancel 分支使用 `current_result`，没有覆盖上下文超限。整流器又把“准备续接”和“成功取得续接流”混为同一个状态转换，在新请求准入前过早重置旧请求元数据。

## 工业级参照

请求准入拒绝发生在新 provider 请求之前，因此不会产生新的流或新 usage；此前已经取得的内容与计费用量仍属于当前运行。资源所有权和状态清理应以实际取得新资源为边界，而不是以计划开始下一次尝试为边界。

## 修复方案

- ReAct 的 `ContextWindowExceededError` 出口复用统一 `_terminal_result(current_result, last_result)`，当前轮有可见成果时优先保留。
- 同一出口通过 `_unaccounted_usage(current_result)` 合并尚未进入累计值的 usage，保持单计。
- `_try_continuations` 仅在 `continue_fn` 成功返回新流、所有权已经取得后调用 `_reset_dead_meta(result)`。预算拒绝、取消或 deadline 发生在新流创建前时，旧请求元数据继续由领域终止出口接管。
- 上下文超限保持终结性：不再续接、不整流、不 fallback，只生成一次 done。

## 决策取舍

没有在预算异常对象上复制旧 usage。该 usage 已由 `StreamResult` 持有，异常重复携带会增加双计风险；保留结果对象到领域出口更符合现有所有权模型。

## 实施记录

修改 `react.py` 的上下文超限分支和 `streaming_rectifier.py` 的续接元数据复位时机。其余续接成功路径仍在新流开始读取前清理旧死流元数据，避免旧 `finish_reason`、`refusal` 或 usage 污染新流。

## 验证

`tests/unit/test_llm_request_budget.py::test_continuation_context_overflow_keeps_current_result_and_usage` 使用真实 `LLMService`、`StreamingRectifier` 和 ReAct 边界复现：初始流产出正文与 usage 后中断，续接预算拒绝；断言 SDK create 仅一次、初始 Reservation 按实际 usage 结算、最终 outcome 保留正文和 usage，且只产生一次 done。

## 教训沉淀

异步恢复链中的“值”既是数据也是所有权凭证。只有成功取得下一阶段资源后，才能清理上一阶段仍用于终止收尾的状态；所有终结性出口应共享同一套 current/last 成果选择与 usage 单计规则。
