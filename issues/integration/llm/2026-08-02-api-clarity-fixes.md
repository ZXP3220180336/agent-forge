# 限流器 API 清晰性：acquire 返回值表述不准确 / async with 用法误导

> **状态**：✅ 已修复（2026-08-02）
> **优先级**：P3（低）
> **来源**：2026-08-01 代码审核（问题 4 + 5）· 2026-08-16 从 limiter.md 提取归档
> **涉及模块**：`app/integration/llm/reservation_limiter.py`（`TokenBucket.acquire` docstring / 移除 `__aenter__`/`__aexit__`）
> **关联文档**：[limiter.md](../../../docs/integration_doc/llm_doc/limiter.md)

---

## 问题描述

### 现象

1. **问题 4：`acquire` 返回值表述不准确**——docstring「总等待时间」与实现不符：实际返回 `wait1 + wait2`（桶内等待），**不含** `retry_after` 的 sleep；调用方集成点忽略返回值；
2. **问题 5：`async with` 用法误导**——docstring 主推 `async with limiter:`，但 `__aenter__` 调 `acquire()` 无参（TPM 桶退化）；`__aexit__` 空操作无释放语义；实际集成点都直接 `await acquire(estimated_tokens=...)`。

### 影响

API 契约与实现脱节，调用方误解返回值语义；`async with` 误导路径（TPM 桶退化）存在。

### 根因

docstring 与实现不一致；`__aenter__/__aexit__` 是无人使用的误导性 API。

---

## 工业级参照

| 库 | 形态 | 按量消耗支持 |
| --- | --- | --- |
| aiolimiter | `async with`（= acquire(1)）+ 带参变体 | 需 `limit(amount)` 绕开局限 |
| limits | 纯方法 `hit()` | 无上下文管理器 |
| Go `x/time/rate` | `Reserve()` → `Wait()` → `CancelAt()` | 预留-取消模型 |

**工业级结论：RPM 按请求计数可用 `async with`；TPM 按量计费应用显式 `reserve/settle` 方法。**

---

## 修复方案（含决策取舍）

**决策**：docstring 明确返回「桶内等待时间」（不含 `retry_after` sleep）；**移除 `__aenter__`/`__aexit__` 死代码**（全项目无 `async with limiter:` 使用方）；`await acquire()` 成为唯一用法。

**取舍理由**：

1. 与 limits、PyrateLimiter 的方法调用形态一致，避免 `async with` 对 TPM 桶的「消耗 1」误导；
2. `reserve/settle`（`reservation_limiter.py`）提供「预留 → 结算 → 退还」完整语义，对齐 Go `Reserve()` 形态；
3. 移除无使用方的死代码，消除误导路径。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/reservation_limiter.py` | docstring 修正返回值语义；移除 `__aenter__`/`__aexit__` | 全量回归 |

---

## 验证

- `await acquire()` 唯一用法；docstring 与实现一致
- 全量测试通过（2026-08-02 修复时验证）

---

## 教训沉淀

- **docstring 必须与实现一致**：返回值语义（桶内等待 vs 总等待）表述不准确误导调用方。
- **TPM 按量计费不用 `async with`**：无参上下文管理器硬编码消耗 1，先天局限——显式 `reserve/settle` 方法（对齐 Go `Reserve` 形态）是正确 API。
