# LLM-046 半流续接缺完成态守卫：EOF 后结算/日志异常被当续接中断重发（重复计费）

> 状态：✅ 已修复 ｜ 优先级：P1（续接成功后被再次续接 → 重复内容 + 双倍计费） ｜ 发现：2026-09-09 ｜ 模块：streaming_rectifier（`_try_continuations`）
> 关联：[LLM-ADR-015](../../../adr/integration/llm/2026-09-03-mid-stream-continuation.md)（半流续接）；与主流路径 `stream_done` 守卫（rectified_stream 迭代收尾）同源同语义；异常穿透后的领域分类见 [REASON-014](../../domain/reasoning/2026-09-09-internal-timeout-misclassified-as-deadline.md)

## 发现

整流主流路径（`rectified_stream`）在 drain 读到 EOF 后置 `stream_done = True`，此后 try 内只剩 `_finish_success`（settle + 成功日志）——其异常（结算或日志侧失败）经 except 顶部的 `stream_done` 守卫**原样上抛**，绝不当作可整流/可续接的流中断（成功流已读完并透传客户端，重发即双倍计费）。

半流续接路径（`_try_continuations` 的续接 drain）**没有同类守卫**：续接流 EOF 后先合并 tool_calls、再调 `_finish_success`；若 `_finish_success`（settle/log）抛异常，落 `except Exception` 被当「续接读取中断」处理——置 `outcome.error`、走 `_finish_interrupted` 再结算 + 记失败日志、`classify_error` 后 `continue` 回边。若该异常恰好被分类为可恢复（RETRYABLE / RATE_LIMITED）且续接预算未耗尽，**同一前缀再次调 `continue_fn`** → 续写流再次从头产出（接缝剥离只兜小尾巴，长重复必然透传客户端），**重复内容 + 双倍计费**。即便分类不可恢复，`outcome.error` 也会被 `_abandon_path` 当作末次中断原因——喂熔断 + 置错误失败信号，掩盖「续接已成功、仅收尾失败」的真实状态。

## 分析

1. **根因**：两条都会把「drain EOF 后收尾段」包在同一个 try 里，但只有整流路径在 try 内设了 EOF 完成态标记供 except 顶部分流。续接路径缺这一分流，把「完成态的收尾异常」与「流中断」混为一谈。
2. **重复的边界**：续接流 EOF 代表该续接请求已成功产出并透传——`continue_fn` 重发是**新的付费请求**，内容从断点（前缀）重算，无法靠幂等吸收。与整流重发不同，续接重复没有「首 token 前」之类防线。
3. **取消/期限与收尾异常同窗**：settle/log 侧失败多为后端偶发（日志后端超时、观测写入异常），恰落在 RETRYABLE 白名单内的概率低但非零；触发即用户可见重复 + 重复扣费，且难排查（无日志指向）。
4. **修复方向收敛**：复用整流路径的完成态守卫语义——`_try_continuations` 的续接 drain 前置 `cont_stream_done = False`，EOF 后、`_finish_success` 前置 `True`，`except Exception` 顶部 `if cont_stream_done: raise`（与 `stream_done` 完全对称）。收尾异常原样上抛，reservation 由 `rectified_stream` 外层 finally 兜底 settle(None)。

## 工业级参照

- **「传递成功」与「收尾成功」必须分离**：流式/消息框架以「终帧（EOF/final chunk）已产出/已 ack」为成功边界——一旦业务 payload 完整送达，ack/commit/日志段的失败**上抛给应用处理**，绝不触发重投（重投 = 用户可见重复 + 重复成本）。本修复即该边界的整流器落地。
- **provider 成本不可回滚**：LLM 续接成功即产生一次不可撤销的计费副作用，重发无法以幂等键吸收（内容不同）——与整流「首 token 前才重发」同因（LLM-035 累积语义：已产出即不可重发），续接成功同样「已收尾即不可再续」。

## 修复

`streaming_rectifier.py::_try_continuations` 续接 drain 段补齐与整流主流路径相同的完成态守卫：

- EOF 前置 `cont_stream_done = False`；EOF 后、`_finish_success` 前置 `True`（与 `stream_done` 的置位点一致：合并 tool_calls 之后、结算前）；
- `except Exception` 顶部 `if cont_stream_done: raise`——`_finish_success`（settle/log）异常原样上抛：不置 `outcome.error`、不喂熔断、不再续接；reservation 未终态由 `rectified_stream` 外层 finally 兜底 settle(None)；
- 该 except 原 `# noqa: BLE001` 随 handler 内含裸 `raise` 变为冗余，移除（与主流路径同形 handler 无 noqa 一致）。

调用方（`_abandon_path` / `rectified_stream`）**零改动**：异常自然穿透 `_abandon_path`（跳过放弃分支——续接成功流不喂熔断）至 `rectified_stream` 外层 finally 后冒泡。

## 验证

- 回归测试 `tests/unit/test_streaming_rectifier.py::test_continuation_finish_success_error_not_retried`（**修复前红**）：monkeypatch `_log_success` 抛 RETRYABLE 收尾异常（`TimeoutError` 子类）——修复前被当续接中断、预算内二次调 `continue_fn`（`continuation_calls == 2`，重复内容）；修复后**异常原样上抛**、`continuation_calls == 1`、`result.content == "部分续写"` 保留、`result.error is None`、熔断 `failures == 0`、reservation 结算 1 次。
- 全量 `uv run pytest`（相关文件 29+46 用例）与 `uv run python -m scripts.verify_alignment` 通过。

## 教训

- **每新增一条「重试/续接恢复路径」，都要自带与既有路径同构的完成态守卫**：EOF 后的收尾段（settle/日志/合并）与「流中断」不同源——前者是「请求已成功」的收尾，后者是「请求未收尾」的可恢复故障；把两者混在一个 except 里分流缺失，必然在某个可恢复异常分类下把成功的请求再发一次。
- **完成态标记置位点要与主流路径一致**：EOF 后、结算前（`stream_done` / `cont_stream_done` 都放在 tool 合并之后、`_finish_success` 之前），保证收尾段任何异常都走「原样上抛」而非恢复分支。

## 跨层分类补强

本问题要求收尾异常原样穿透 Integration，但内置 `TimeoutError` 到达 ReAct 后曾被无条件解释为总执行硬超时。该领域分类缺口已在 [REASON-014](../../domain/reasoning/2026-09-09-internal-timeout-misclassified-as-deadline.md) 修复：ReAct 只在自己的 timeout scope `expired()` 时归 TIMEOUT，其余同名异常归 UNKNOWN；真实 `LLMService + 续接 + ReAct` 回归同时锁定“不再续接、分类正确、部分内容与 usage 保留”。
