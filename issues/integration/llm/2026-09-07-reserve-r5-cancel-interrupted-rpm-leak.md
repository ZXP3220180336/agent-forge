# LLM-042 reserve R5 兜底退款被二次取消时 RPM 配额泄漏

> 发现：2026-09-07 ｜ 状态：✅ 已修复 ｜ 模块：reservation_limiter（`ReservationLimiter._acquire` R5）
> 关联：fallback/副模型守卫 [LLM-041](2026-09-06-fallback-window-and-quota.md)（同批取消/收尾治理）· 取消中断未终态设计 [settle-cancel 并发互斥 LLM-010](2026-08-16-settle-cancel-concurrent-race.md)

## 发现

审查 `ReservationLimiter._acquire` 的 R5 兜底分支（TPM 预留被取消 → `res.cancel()` 回退已扣 RPM）时怀疑：若 `res.cancel()` 的退款本身在 await 点再次被取消，cancel 保持未终态，而 **reserve 的 Reservation 不传出 `_acquire`、没有外层持有者续退**——与「未终态供外层续退」契约的适用前提冲突，RPM 可能永久不回满。

补失败测试探针（`test_reserve_r5_refund_interrupted_by_cancel_leaks_rpm`）：RPM 桶 `refund` 挂起（暴露退款 await 点）期间对 reserve 任务 `task.cancel()` 二次取消 → 断言桶回满。**修复前红**：`rpm._tokens == 4 ≠ 5`，泄漏属实。

## 分析

1. **两层取消语义**：`asyncio` 中任务取消是「请求」，作用于当前 await 点。TPM `acquire` 排队等待被取消是第一层；R5 退款 `refund` 的 await（桶锁竞争/内部 await）被再次取消是第二层。第二层把 `Reservation.cancel` 中断在退款循环中 → `_settled` 保持 False（设计如此，供外层续退）。
2. **外层续退前提不成立**：既有设计（[settle/cancel 中途取消保持未终态](../../../tests/unit/test_reservation_limiter.py)）默认 Reservation 由调用方持有——`generate`/整流器在 create 后的 `finally` 兜底续退。但 `_acquire` 是 **reserve 内部一次性流程**：res 既不外传、调用方也拿不到，取消中断即丢弃，无兜底机会。
3. **工业级参照**：异步资源清理须抵抗自身被取消——标准做法是 `asyncio.shield`（把退款子任务从取消中隔离开，清理完成才让取消传播）或先 `ensure_future` 启动再 `gather` 收尾。另一种是 Python 3.11+ 的「取消是单次请求」语义：一次取消只打断一个 await，处理后可继续后续 await。
4. **方案取舍**：
   - A（shield/gather 进 `Reservation.cancel`）：取消时也保证全部桶退完。改动共享契约面大，且与「未终态供外层续退」语义冲突——该契约对 `settle`（外传、有兜底）场景是正确的。
   - B（**采用**，R5 就地循环补齐）：`_acquire` 的 R5 分支循环调用 `res.cancel()` 直到 `settled` 再传播原取消。只改 reserve 这一「无外层续退」死角，不动 `Reservation.cancel` 既有契约；cancel 幂等 + `TokenBucket.refund` 容量封顶保证重复退款安全；非取消 `BaseException` 不吞（仅对 `CancelledError` 续试）。

## 修复

`app/integration/llm/reservation_limiter.py` `_acquire` R5 分支：

```python
except BaseException:
    # 防 R5：TPM 预留前被硬取消 → 回退已扣的 RPM。
    # 回退本身也可能被再次取消（refund await 被打断、cancel 保持未终态）。
    # 与 settle/cancel 的「未终态供外层续退」不同：reserve 的 res **不传出
    # 本函数**、没有外层兜底——须就地循环补齐退款到终态再传播原取消
    # （cancel 幂等 + refund 容量封顶，重复退款安全）。
    while not res.settled:
        try:
            await res.cancel()
        except asyncio.CancelledError:
            continue  # 退款途中再被取消 → 下一轮继续补齐
    raise
```

## 验证

- 探针测试转绿：`test_reserve_r5_refund_interrupted_by_cancel_leaks_rpm`（refund 挂起期间二次取消 → RPM 桶回满）。
- 既有「未终态供外层续退」语义测试（`test_settle_cancelled_midway_keeps_unsettled_and_cancel_refunds_all` / `test_cancel_cancelled_midway_keeps_unsettled` / `test_concurrent_settle_cancel_mutex_no_double_refund`）零改动通过——证明修复未破坏共享契约。
- 受影响回归：reservation_limiter 39 passed、集成组 109 passed；全量 `uv run pytest` 856 passed；`verify_alignment` 通过。

## 教训

- **「未终态供外层续退」有适用前提（reservation 必须被外层持有）**：资源收尾设计标注契约时要区分「所有权外传（有兜底方）」与「生命周期内部一次性（无兜底方）」——后者必须就地保证收尾，不能假设外层能续退。
- **取消的测试要命中真实时序**：桩「refund 主动抛 CancelledError」模拟不了「外部取消打在退款 await 点」——用「refund 挂起 + 对任务 `task.cancel()`」的并发门控才能复现二次取消语义（既有 `_CancelOnRefundBucket` 桩只适配串行逐桶模型）。
- **清理抵抗取消是异步资源管理的通用要求**：`asyncio` 取消传导会打断任何 await，包括清理自身；工业级用 `shield`/独立子任务隔离清理，本项目在无外层兜底死角用「循环补齐 + 单次取消语义」等效实现，代价更小。
