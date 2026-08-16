# 半开探针语义演进：进重试循环、OPEN 误关、死锁、429/4xx 判定、槽位泄漏、取消

> **状态**：✅ 已修复（2026-08-05 定型，2026-08-09 槽位泄漏，2026-08-12 取消语义）
> **优先级**：P0（严重，熔断自动恢复正确性）
> **来源**：2026-08-01~08-12 工业级改造（问题 4 系列 + 半开语义演进）· 2026-08-16 从 retry.md 提取归档
> **涉及模块**：`app/integration/llm/retry.py`（`CircuitBreaker` 半开 / `RetryHandler._probe_attempt`）
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md)

---

## 问题描述

半开探针（熔断恢复探测）语义逐步演进，累积多个缺陷：

| # | 问题 | 后果 |
| --- | --- | --- |
| 1 | **探针进入重试循环（4a）** | 探针失败 → OPEN → 继续重试，把一次探测放大成多次调用，干扰恢复判断 |
| 2 | **OPEN 下 `record_success()` 误关熔断（4b）** | OPEN 下收到成功（重试泄漏/fallback）走"重置为 CLOSED"兜底分支 → 熔断被误关 |
| 3 | **半开死锁：探针 429/4xx 不推进状态机** | 探针被放行（槽位递增）但 429/4xx 既不成功也不失败 → 槽位耗尽后 `allow_request()` 恒 False，熔断器卡死 HALF_OPEN |
| 4 | **429 计为探针成功（曾尝试）** | 429 被当成功 → 误关熔断器流量涌入过载下游 |
| 5 | **4xx 探针误回 OPEN（2026-08-05）** | 4xx 一律 `record_failure()` → 半开阶段客户端错误把熔断器反复打回 OPEN，下游即使恢复也永远无法完成健康探测（横跳） |
| 6 | **探针槽位泄漏（2026-08-09）** | `_probe_attempt` 仅 `except Exception`：取消/`BaseException` 中断绕过 `release_probe`/`record_failure` → 槽位永久占用，卡死 HALF_OPEN |
| 7 | **探针取消语义（2026-08-12）** | 探针取消后回 OPEN 还是归还槽位——取消的探针没有证明下游恢复 |

### 影响

熔断自动恢复失效（死锁/横跳/误关），下游恢复后无法完成健康探测。

---

## 工业级参照（Hystrix / Resilience4j）

| 议题 | 工业结论 | 本项目立场 |
| --- | --- | --- |
| 探针是否重试 | Hystrix「only the first request after sleep window」；Resilience4j 半开直打主服务 | **探针单次调用，不重试** |
| 429 处理 | 多数派计失败触发熔断；少数派不计 | **CLOSED 排除 429**（只退避）；**半开探针 429 是下游过载信号 → 回 OPEN + 冷却** |
| 4xx 处理 | 中性：4xx 是调用方 bug，不算 provider 故障 | **4xx 不改变状态 + `release_probe()` 归还槽位 + 抛上层**（客户端问题，等待正常请求探测） |
| 探针失败是否降级 | fallback 是方法级包装，探针失败必然触发 | **探针失败仍降级**（fallback 纯兜底） |
| 探针取消 | Hystrix「取消 = 探针失败回 OPEN」 | **取消 → `record_failure()` 回 OPEN**（探针没跑完 = 无法证明恢复 = 按失败处理） |

---

## 修复方案（含决策取舍）

**决策**：半开状态走 `_probe_attempt` 单次调用；任何探针结果必然推进状态机；`accounted` 标志 + finally 兜底槽位永不泄漏。

**修复要点**：

1. **探针单次调用不重试**：`_probe_attempt`（HALF_OPEN 专属）；
2. **OPEN 下 `record_success()` 为 no-op**：不据此关闭熔断器（恢复只能由主链路探针验证）；
3. **三分类处理**：成功→`record_success()` 累计达阈值关闭；429/超时/5xx→`record_failure()` 回 OPEN + 冷却（停止探测让下游喘息）；4xx/未知→不改变状态 + `release_probe()` 归还槽位 + 抛上层；
4. **槽位永不泄漏**：`_probe_attempt` 改 `try/finally` + `accounted` 标志——任何未记账退出路径（SystemExit/KeyboardInterrupt/自定义 BaseException）由 finally `release_probe()` 归还槽位；`CancelledError` 单独捕获 → `record_failure()` 回 OPEN + 立即传播（不尝试 fallback）；
5. **取消回 OPEN**（对齐 Hystrix）：探针没跑完 = 无法得出成功结论 = 按失败处理；
6. **槽位记账同步**：探针失败回 OPEN 时清零 `_half_open_requests`（OPEN 不残留半开记账）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/retry.py` | `_probe_attempt` 单次调用 + 三分类 + `accounted`/finally 兜底 + OPEN no-op | `test_retry.py` 半开探针（成功/429/4xx/取消/BaseException）用例 |

---

## 验证

- 半开探针各结果推进状态机，无死锁；槽位泄漏路径有 `test_half_open_probe_other_baseexception_releases_slot` 固化
- 全量测试通过（2026-08-05 定型 + 08-09/08-12 修正时验证）

---

## 教训沉淀

- **任何探针结果必须推进状态机**：成功→连续成功，失败→回 OPEN 或归还槽位——不存在"放行后不记录"路径，杜绝死锁。
- **429 是过载信号、4xx 是客户端问题**：429 探针回 OPEN（停止探测让下游喘息）；4xx 归还槽位（不横跳、等待正常请求）。
- **探针取消 = 失败回 OPEN**（Hystrix 语义）：探针没跑完无法证明恢复，按失败处理；`accounted` + finally 兜底保证槽位永不泄漏。
