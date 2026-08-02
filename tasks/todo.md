# RateLimiter 审核问题修复计划

> 来源：`docs/llm/rate_limiter.md` 附录「2026-08-01 代码审核记录」6 个遗留问题。
> 方式：**逐个修复**，每修完一个停下来总结并更新文档。

## 进度

- [x] **问题 1（严重）** 配置 0 除零崩溃 —— `TokenBucket.acquire` 对 `refill_rate <= 0` 防御，直接放行
- [x] **问题 2（中）** 持锁 sleep —— `acquire` 重构为「锁内计算 → 锁外 sleep → 循环重检」（连带解决问题 6）
- [x] **问题 3（中）** TPM 只算 prompt —— `_count_prompt_tokens` 加 `max_tokens` 输出余量
- [x] **问题 4（低）** `acquire` 返回值表述不准 —— 修正 docstring
- [x] **问题 5（低）** `async with` 用法误导 —— 移除 `__aenter__/__aexit__` 死代码 + 更新 docstring
- [x] **问题 6（低）** `_tokens` 轻微为负 —— 已由问题 2 重构连带解决（只在 `_tokens >= tokens` 时扣减）

## 评审（2026-08-02 全部完成）

6 个审核问题全部修复，测试 13/13 通过（rate_limiter）+ 37/37（stream_rectify + retry）无回归。

| 问题 | 修复方式 | 验证 |
|---|---|---|
| 1 | `TokenBucket.acquire` 对 `refill_rate <= 0` 直接放行 | `test_bucket_zero_refill_disabled` |
| 2 | 锁外 sleep 循环重检 | `test_bucket_wait_does_not_block_others` / `test_bucket_cancel_does_not_corrupt_state` |
| 3 | `_count_prompt_tokens` 加 `max_tokens` 输出余量 | 37 测试无回归 |
| 4 | docstring 明确返回值语义 | 纯文档 |
| 5 | 移除 `__aenter__/__aexit__` 死代码 | `py_compile` + 全测试 |
| 6 | 由问题 2 重构连带解决 | `test_bucket_cancel_does_not_corrupt_state` 覆盖 |

## 关联文件

| 文件 | 改动 |
|---|---|
| `app/services/llm/rate_limiter.py` | TokenBucket.acquire / RateLimiter.acquire / docstring |
| `app/services/llm/llm_service.py` | `_count_prompt_tokens` 输出余量（问题 3） |
| `tests/unit/test_rate_limiter.py` | 新增各问题回归测试 |
| `docs/llm/rate_limiter.md` | 附录问题标记修复 + 正文已知边界同步 |

## 评审

（待各问题修复后逐条补充）
