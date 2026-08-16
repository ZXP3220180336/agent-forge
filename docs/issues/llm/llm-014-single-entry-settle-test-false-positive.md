# LLM-014 单条目 settle 测试假阳性，settle 退款从未被真实验证

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P2（测试）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 14）
> **涉及模块**：`tests/unit/test_reservation_limiter.py`（`test_reservation_settle_refunds_difference`）
> **关联文档**：[limiter.md](../../integration_doc/llm_doc/limiter.md)

---

## 问题描述

### 现象

`test_reservation_settle_refunds_difference` 用 `_reserve_single(b, 10)` 构造**单条目**（仅 RPM 桶）。而 `Reservation.settle` 只退 `entries[1:]`（按量桶），单条目时 `entries[1:]` 为空 → **settle 实际不退款**。测试断言 `await b.acquire(96)` 通过，是因为桶 `refill_rate=100/s` 在 60ms 内自动补满 6 个 token（断言不检查等待时长）——**靠时间推进而非 settle 退款**。

### 影响

单条目形态下 settle 的退款逻辑从未被真实验证——若 `settle` 退款实现回归（如退错桶/不退），此测试仍通过（假阳性）。

### 根因

单条目（仅按次桶）不触发 settle 退款（settle 只退按量桶），测试断言依赖桶 refill 而非退款生效。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 测试有效性 | 断言必须验证目标行为（退款生效），而非依赖无关机制（桶时间 refill）；否则假阳性 |
| 本项目组合形态测试 | `test_reservation_limiter_settle_tpm_only`（RPM+TPM 双条目）已验证真实退款——单条目测试应对齐 |

**核心**：单条目不触发 settle 退款，测试应改用双条目（按次桶 + 按量桶）显式验证 TPM 退款。

---

## 修复方案（含决策取舍）

**决策**：`test_reservation_settle_refunds_difference` 改用**双条目**（RPM 按次桶 + TPM 按量桶），显式断言退款发生——`rpm.refunds == 0`（按次桶 settle 不退）、`tpm.refunds == 1`（按量桶退 1 次），并保留 `res.settled` 断言。

**取舍理由**：

1. 双条目触发 settle 真实退款（`entries[1:]` 非空），断言退款生效而非依赖 refill；
2. `refund` 计数断言直接验证「按次桶不退、按量桶退」的语义；
3. 与 `test_reservation_limiter_settle_tpm_only`（已验证真实退款）语义一致。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `tests/unit/test_reservation_limiter.py` | `test_reservation_settle_refunds_difference` 改双条目（RPM 按次桶 + TPM 按量桶）+ refund 计数断言（`rpm.refunds==0` / `tpm.refunds==1`） | 修正 1 用例 |
| 文档 | [llm.md](../../integration_doc/llm_doc/llm.md)（已实现列表加 LLM-014 条目） | — |

---

## 验证

- `tests/unit/test_reservation_limiter.py` **35 passed**（含修正后的 settle 退款断言）
- 全量测试 **364 passed**（46.14s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **断言必须验证目标行为**：settle 测试靠桶 refill 补满通过是假阳性——退款逻辑从未被验证。改用双条目 + refund 计数断言，直接验证「按次桶不退、按量桶退」。
- **单条目是 settle 的退化形态**：settle 只退 `entries[1:]`，单条目（仅按次桶）不退款——测试单条目 settle 退款无意义，必须双条目。
