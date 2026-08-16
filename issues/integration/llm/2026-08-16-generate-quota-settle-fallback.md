# LLM-002 非流式 generate() 配额结算无兜底

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P0（合并前必修）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 2）
> **涉及模块**：`app/integration/llm/llm_service.py`（非流式 `generate()`）
> **关联文档**：[llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md)（对称的 finally 兜底模式）

---

## 问题描述

### 现象

非流式 `generate()` 的解析 + 结算阶段无 try/finally 兜底：

1. `StreamParser.parse_non_stream(response)` 抛异常（响应形状异常）时，`active["res"]` 仍未 settle/cancel，无人清理；
2. `await res.settle(...)` 期间被硬取消（`CancelledError`），res 已从 `active` 弹出且未到终态（`reservation_limiter` 的终态标记在退款完成后才置位），无人续退。

### 影响

TPM/RPM 配额永久占用，长期可导致本地限流饿死；与流式 [rectified_stream 的 finally 兜底](../../../docs/integration_doc/llm_doc/streaming_rectifier.md)（`streaming_rectifier.py:261-265`）行为不对称。

### 根因

非流式路径缺少与流式路径对称的「finally 兜底 cancel」——请求已发出后的结算阶段异常/取消没有统一出口。

---

## 工业级参照

Python asyncio 官方推荐协程用 `try/finally` 保证 `CancelledError` 时清理逻辑必定执行；`CancelledError` 在 await 点抛出，finally 是清理必经路径；清理后应 re-raise（不吞掉取消信号）。

| 参照 | 做法 |
| --- | --- |
| Python 官方 asyncio 文档 | 协程用 `try/finally` 稳健执行清理逻辑；`Task.cancel()` 是优雅关闭，在 await 点抛 `CancelledError` |
| async context manager 最佳实践 | `async with` / `asynccontextmanager` 内部即 try/finally；`CancelledError` 时 finally 仍执行；不 swallow `CancelledError`，清理后 re-raise |
| 本项目流式路径 | `rectified_stream` finally 兜底 cancel（R1 设计）——非流式应对齐 |

---

## 修复方案（含决策取舍）

**决策**：`generate()` 的解析 + 结算阶段放入 `try/finally`——成功路径 `settle(actual)` 退 TPM 差；解析抛异常 / settle 被取消 → 未终态 res `cancel()` 全额退，随后 re-raise。

**取舍理由**：

1. 与流式 `rectified_stream` finally 兜底完全对称（直接对齐审核报告「与流式路径行为不对称」）；
2. `try/finally` 是 asyncio 官方推荐、async 资源清理的公认模式，非发明新机制；
3. 不 swallow `CancelledError`——清理后 re-raise，保持取消信号传播语义。

**语义边界**：
- parse 失败时 `sr.usage` 为 None → `settle(None)` 保留全部预留 + 标记终态（请求已发出，不退 RPM，账务保守正确）；
- settle 被取消 → `cancel()` 全额退（`TokenBucket.refund` capacity 封顶保证重复退款安全）+ re-raise；
- 正常路径行为不变（settle 退 TPM 差）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/llm_service.py` | `generate()` 解析 + 结算放 `try/finally`；finally 内 settle，except 兜底 cancel + re-raise | `test_llm_service.py` 新增 `test_generate_settles_on_parse_error` + `test_generate_cancels_when_settle_cancelled` |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表加 LLM-002 条目） | — |

---

## 验证

- `tests/unit/test_llm_service.py` **5 passed**（含新增 2 条兜底用例）
- 全量测试 **352 passed**（41.40s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **异步资源结算必须 try/finally**：任何「请求已发出、待结算」的资源，结算阶段必须放 finally（或 `async with`），否则异常/取消路径泄漏——流式与非流式都要有统一兜底。
- **「已发出请求」的配额语义**：请求已发出后 `settle(None)`（保留）而非 `cancel()`（全额退）——RPM 是真实消耗，退回会导致客户端配额虚增（详见 LLM-001 教训的延伸）。
