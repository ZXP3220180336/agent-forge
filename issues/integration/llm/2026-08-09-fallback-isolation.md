# fallback 隔离：成败不进熔断状态机 / fallback 异常覆盖主调用

> **状态**：✅ 已修复（2026-08-09）
> **优先级**：P1（中，熔断语义正确性）
> **来源**：2026-08-01 工业级改造（问题 4 深化）+ 2026-08-09 异常覆盖修正 · 2026-08-16 从 retry.md 提取归档
> **涉及模块**：`app/integration/llm/retry.py`（`RetryHandler` fallback 路径）
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md)

---

## 问题描述

### 现象

1. **fallback 成败进入熔断状态机**：fallback 成功清零熔断计数（主链路持续故障永不熔断）；fallback 失败累计熔断计数（误判主链路故障）；熔断 OPEN 期 fallback 被当作主链路传入单次调用路径；
2. **fallback 异常覆盖主调用异常（2026-08-09）**：fallback 也失败时 `last_exc = e` 被 fallback 异常覆盖，最终抛 fallback 异常——熔断窗口记录的是主链路，上层按异常类型判定重试/降级/日志时与熔断器记录不一致。

### 影响

fallback 干扰熔断判定（主链路持续故障永不熔断/误熔断）；上层拿到错误异常类型。

### 根因

fallback 与主链路未隔离——fallback 成败不应进入熔断状态机；fallback 异常不应覆盖主异常。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| fallback 隔离契约 | 熔断器只观察主链路（`call_fn`）的健康——备用链路通不能证明主链路恢复，备用链路故障也不代表主链路故障 |
| 异常链 | fallback 失败保留主异常为主，fallback 异常链 `__cause__` 保留诊断 |

---

## 修复方案（含决策取舍）

**决策**：fallback 纯兜底——成功直接返回、失败自然抛出，不调用 `record_success`/`record_failure`；fallback 失败时 `raise last_exc from fallback_exc`（主异常为主）。

**修复要点**：

1. **fallback 成败不进熔断状态机**：成功返回（不清零熔断窗口）、失败自然抛出（不累计窗口、不改写冷却计时）；
2. **熔断 OPEN 拒绝路径**：fallback 走纯兜底（单次、不重试、不触碰熔断器）；
3. **异常链**：`raise last_exc from fallback_exc`——主调用异常为主（上层按它判定语义），fallback 异常 `__cause__` 保留；CLOSED 重试路径与 HALF_OPEN 探针路径同改，熔断 OPEN 拒绝路径主调用未执行不改。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/retry.py` | fallback 路径隔离熔断器 + `raise last_exc from fallback_exc` | `test_retry.py` fallback 成功不重置熔断器/失败不计数/异常链用例 |

---

## 验证

- fallback 成败不触碰熔断器；fallback 失败抛主异常（`__cause__` 链）
- 全量测试通过（2026-08-09 修正时验证）

---

## 教训沉淀

- **熔断器只观察主链路**：fallback 是备用链路，成功不稀释主链路错误率、失败不代表主链路故障——隔离契约保证熔断判定准确。
- **异常链语义统一**：fallback 失败抛主异常（`__cause__` 链），上层拿到的异常类型与熔断器记录一致。
