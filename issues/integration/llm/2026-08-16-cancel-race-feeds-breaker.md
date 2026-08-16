# LLM-011 整流放弃分支 cancel 竞态错误喂熔断器

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 10）
> **涉及模块**：`app/integration/llm/streaming_rectifier.py`（迭代放弃分支）
> **关联文档**：[streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md) · [llm-001](2026-08-16-stream-error-propagation.md)（取消不计入熔断）

---

## 问题描述

### 现象

`_should_rectify` 因 `cancel_event.is_set()` 返回 False（用户取消）后，迭代异常处理统一走「放弃分支」：`if classify_error(e) == RETRYABLE: retry.circuit_breaker.record_failure()` + 产出「流式响应中断」事件。当异常恰为 RETRYABLE（超时/5xx）时，**用户取消被计入熔断窗口**。

### 影响

用户取消（非下游故障）喂熔断器，与 `test_cancel_event_not_feeds_breaker` 声明的契约相悖——取消是客户端主动终止，不代表下游故障，误喂会加速熔断、影响其他请求。

### 根因

放弃分支未区分「_should_rectify 因何返回 False」——取消导致的 False 与其他原因（已产出/超限/非可恢复）统一走喂熔断逻辑。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| LLM-001 已确立原则 | 用户取消（`cancel_event`）非下游故障，**不喂熔断器**（与 429 不计入同理：都是客户端侧/非下游证据） |
| 本项目熔断器语义 | `record_failure` 只记录「下游故障证据」（超时/5xx）；用户取消是客户端主动行为，非证据 |

**核心**：放弃分支必须区分「取消导致的放弃」与「故障导致的放弃」——取消走取消路径（不喂熔断），故障才喂熔断。

---

## 修复方案（含决策取舍）

**决策**：放弃分支喂熔断前补一道 `if cancel_event and cancel_event.is_set():` 守卫——置位则产出取消事件 + `return`（不喂熔断、标记 `result.error="用户取消"`），与整流退避后第二道守卫对称。

**取舍理由**：

1. 对齐 LLM-001「取消不计入熔断」契约（`test_cancel_event_not_feeds_breaker`）；
2. 与整流退避后守卫对称（放弃分支同样先判取消）；
3. 覆盖竞态窗口：cancel 在循环顶部检查后、迭代阶段置位（LLM-006 只覆盖循环入口，此守卫覆盖迭代放弃路径）。

**语义边界**：

- cancel 置位 + RETRYABLE 异常 → 取消路径（不喂熔断 + 取消事件 + `result.error="用户取消"`）；
- cancel 未置位 + RETRYABLE → 正常放弃（喂熔断 + 中断事件）；
- 非可恢复异常 → 不喂熔断（classify_error 判定，不变）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/streaming_rectifier.py` | 放弃分支喂熔断前加 `cancel_event` 守卫（取消 → 取消事件 + `result.error`，不喂熔断） | `test_streaming_rectifier.py` 新增 `test_cancel_during_iteration_with_retryable_exc_does_not_feed_breaker` |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表加 LLM-011 条目） | — |

---

## 验证

- `tests/unit/test_streaming_rectifier.py` **12 passed**（含新增 cancel 竞态不喂熔断用例）
- 全量测试 **364 passed**（42.16s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **「因何放弃」必须区分**：`_should_rectify` 返回 False 的原因多样（取消/已产出/超限/非可恢复），放弃分支不能统一处理——取消走取消路径，故障才喂熔断。
- **取消守卫要覆盖每个放弃出口**：循环入口（LLM-006）+ 整流退避后 + 迭代放弃（本修复）——取消信号在每条路径都被响应，且不污染熔断窗口。
