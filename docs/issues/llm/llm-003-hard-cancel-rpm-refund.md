# LLM-003 流式硬取消对「已发出请求」退回 RPM 配额

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 3）
> **涉及模块**：`app/integration/llm/streaming_rectifier.py`（迭代阶段 `finally` 兜底）· `app/integration/llm/llm_service.py`（`_rate_limited_call` 契约）
> **关联文档**：[streaming_rectifier.md](../../integration_doc/llm_doc/streaming_rectifier.md) · [limiter.md](../../integration_doc/llm_doc/limiter.md)（reserve/settle 语义）

---

## 问题描述

### 现象

流式 `rectified_stream` 迭代阶段被硬取消（`CancelledError`）时，`finally` 兜底对未终态 res 调 `await res.cancel()`——全额退回 RPM 配额。但此刻 **create 已成功、请求已真实到达服务端**，RPM 是真实消耗。

同一事件的两条路径账务不一致：

| 取消路径 | 结算 | RPM |
| --- | --- | --- |
| 优雅取消（`cancel_event` 置位，迭代内检查） | `_settle_active` → `settle(usage)` | 保留 |
| 硬取消（`CancelledError`，FastAPI 断连等高频场景） | `finally` → `cancel()` | **退回** |

### 影响

RPM 桶被虚增 → 客户端以为还有配额 → 突发请求超过服务端限额 → 429 重试风暴（恰是本模块要防的）。与 `reservation_limiter.py:216-222` `cancel()` docstring「请求已真实发生则 RPM 是真实消耗」的契约相悖。

### 根因

`finally` 兜底对「请求已发出的 reservation」错误地走了 `cancel()`（回滚语义），而非 `settle(None)`（提交语义）。实际上 `finally` 里的 res 必然代表「请求已发出」——create 失败会在 `_rate_limited_call` 内 `cancel()` + `pop()`，create 阶段的 `CancelledError` 在 create 阶段就传播、不会进入迭代 `finally`。

---

## 工业级参照

| 参照 | 做法 | 对应本项目 |
| --- | --- | --- |
| SQLAlchemy 事务（[commit a845da8](https://github.com/sqlalchemy/sqlalchemy/commit/a845da8b0fc5bb172e278c399a1de9a2e49d62af)，issue #7388） | `Session.commit()` 被 `CancelledError` 中断后事务已处于 committed 状态，外围 context manager 再 rollback 会报错——**修复：检查事务状态，已提交则跳过回滚**。「已提交的副作用是 CancelledError 无法撤销的永久副作用」 | 请求已发出 = 已提交副作用 → `settle(None)`（提交）而非 `cancel()`（回滚） |
| Python asyncio 官方 | 取消时 `finally` 清理 + re-raise（不吞取消信号）；**取消态 `finally` 只能安全执行一个 awaitable 清理**（多个顺序 await 会因 cancelling 状态被再次取消，见 [asyncpg#772](https://github.com/MagicStack/asyncpg/issues/772)） | `settle(None)` 内部无退款 await 循环（`actual=None` 跳过），是安全的单 await 清理；`cancel()` 的 refund 循环多 await 有部分退款后中断的泄漏风险 |
| token-throttle（客户端限流） | 未发出的请求 refund 恢复配额；已实际消费的容量不恢复。「reserve immediately before dispatching」 | 请求已发出（dispatch 后）→ 不 refund，保留配额 |

---

## 修复方案（含决策取舍）

**决策**：`rectified_stream` 迭代阶段 `finally` 兜底由 `await res.cancel()` 改为 `await res.settle(None)`。

**取舍理由**：

1. `finally` 里的 res 必然代表「create 已成功、请求已发出」——`_rate_limited_call` 在 create 失败时已 `cancel()` + `pop()`，create 阶段的 `CancelledError` 在 create 阶段传播、不进入迭代 `finally`；故此处 `settle(None)`（提交语义）正确；
2. 与优雅取消路径（`_settle_active` → settle）账务一致（都保留 RPM）；
3. `settle(None)` 内部无退款 await 循环，规避取消态 finally「多 await 清理被再次打断」的 asyncio 陷阱，比 `cancel()` 更安全；
4. `settle(None)` 保留全部预留 + 标记终态 + 不触发估算器回调（`actual=None`），账务保守正确、不污染样本分布。

**语义边界**：

- create 前/失败路径不变（`_rate_limited_call` 内 `cancel()` 全额退——请求未发出，正确）；
- 正常结束 / 整流成功 / 优雅取消路径不变（`settle(usage)` 退 TPM 差）；
- 硬取消路径：`settle(None)` 保留配额 + 终态，不泄漏（与 LLM-002 确立的「已发出请求 → settle 保留」语义统一）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/streaming_rectifier.py` | 迭代阶段 `finally` 兜底 `cancel()` → `settle(None)`（保留配额 + 终态） | `test_streaming_rectifier.py` 硬取消 + settle 中途取消用例改为断言 `settle(None)`（`cancel_calls==0`） |
| `app/integration/llm/llm_service.py` | `generate()` finally 的 settle 取消兜底 `cancel()` → `settle(None)`（全项目统一「已发出请求保留配额」） | `test_llm_service.py` 新增 `test_generate_settles_when_settle_cancelled` |
| 文档 | [llm.md](../../integration_doc/llm_doc/llm.md)（已实现列表 + 修正 LLM-002 条目）/ [streaming_rectifier.md](../../integration_doc/llm_doc/streaming_rectifier.md)（结算闭环表 + R2 注释） | — |

---

## 验证

- `test_streaming_rectifier.py` / `test_stream_rectify.py` / `test_llm_service.py` **37 passed**
- 全量测试 **352 passed**（42.26s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过（含文档链接死链校验）

---

## 教训沉淀

- **「已发出的副作用」不可回滚**：请求已发出（create 成功）后取消，配额按提交语义处理（保留），而非回滚（退回）——对齐 SQLAlchemy「已提交事务不可 rollback」；否则客户端配额虚增，自欺欺人地触发服务端 429。
- **取消态 finally 清理用无 await 或单 await 操作**：`settle(None)` 比 `cancel()`（refund 循环多 await）更适合取消兜底——多 await 清理在 cancelling 状态下可能中断于中途，导致部分退款 + 未终态。
