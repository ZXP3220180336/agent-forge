# LLM-006 整流重试前不检查 cancel_event，取消后仍发真实请求

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 4）
> **涉及模块**：`app/integration/llm/streaming_rectifier.py`（`rectified_stream` 整流循环）
> **关联文档**：[streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md) · [llm-001](llm-001-stream-error-propagation.md)（取消信号语义）

---

## 问题描述

### 现象

`rectified_stream` 的整流循环 `for attempt in range(...)` 直接进入 `retry.execute(create_fn)`——`create_fn` 内部执行真实 reserve + API 请求。取消信号（`cancel_event`）只在**迭代阶段**（收到第一个 chunk 时）检查：用户已取消后，下一次整流 attempt 仍会发起真实请求，直到收到 chunk 才终止。

### 影响

用户已取消的请求仍计费、占用配额；取消语义不彻底（与「取消后立即停止」的预期不符）。

### 根因

整流循环入口（每次 attempt 的 create 前）缺 `cancel_event` 检查——取消是协作式的，代码必须在副作用（发起请求）前主动检查。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| Orleans（Microsoft） | 「调用前检查 token 是否已取消，若已取消立即抛异常**且不发请求**」 |
| Swift Concurrency 教程 | 「在循环顶部 / 副作用前检查取消——已取消则不'白做'」 |
| Tokio PR #7462 | 修复循环：token 已取消时不再 poll 新 future（检查放在 poll 之前） |
| C# async/await | 取消是协作式，操作在下个 checkpoint 停止；循环边界必须显式检查 |

**核心**：取消后不得发起新请求/副作用；在循环边界（每次 attempt 入口）显式检查。

---

## 修复方案（含决策取舍）

**决策**：在整流循环 `for attempt` 顶部加 `cancel_event` 检查——置位则产出取消事件 + `return`，不调用 `create_fn`。

**取舍理由**：

1. 对齐工业界「取消后不发起新副作用」（Orleans / Swift / Tokio）；
2. 覆盖首次尝试（cancel 已置位时不发请求）与整流重试入口（上一轮中断后取消不再整流）；
3. 与现有迭代内检查、整流退避后检查形成三道守卫（取消信号最快生效）。

**语义边界**：

- 取消返回前不调用 `create_fn` → 无新 reserve/请求（不发费、不占配额）；
- 上一 attempt 的 res 已由 `_finish_interrupted` / `finally` 结算，循环顶部 active 干净，取消返回无泄漏；
- 正常路径（cancel 未置位）行为不变。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/streaming_rectifier.py` | `for attempt` 循环顶部加 `cancel_event` 检查（置位 → 取消事件 + return）；整流退避后检查补 `result.error = "用户取消"`（与取消路径一致） | `test_stream_rectify.py` 更新 `test_cancel_event_no_rectify`（calls 1→0）+ 新增 `test_cancel_during_rectify_stops_new_attempt`；`test_streaming_rectifier.py` 更新 `test_cancel_event_no_rectify` |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表加 LLM-006 条目） | — |

---

## 验证

- `test_stream_rectify.py` / `test_streaming_rectifier.py` **33 passed**（含新增整流取消用例）
- 全量测试 **359 passed**（45.34s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **副作用前必须检查取消**：整流重试入口（create 前）是「发起真实请求」的副作用点——取消信号必须在副作用前生效，否则「用户取消仍计费」。
- **协作式取消的三道守卫**：循环入口（本修复）+ 迭代内 + 整流退避后——取消信号在流生命周期每个边界都能最快被响应。
