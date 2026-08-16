# LLM-005 retry 熔断器并发路径零测试覆盖

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P2（质量/测试缺口）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 5）
> **涉及模块**：`app/integration/llm/retry.py`（`CircuitBreaker` / `RetryHandler`）· `tests/unit/test_retry.py`
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md)

---

## 问题描述

### 现象

`RetryHandlerManager` 按 model_key 跨请求共享 `CircuitBreaker`（滑动窗口 / 半开探针 / 槽位计数），文档宣称「asyncio 单线程无需加锁、方法间状态交错是设计允许的」——但**无任何测试验证并发路径**：并发探针交错、`allow_request()` 与 `record_*` 交错时的槽位/计数正确性。

### 影响

「无锁安全」不变量无测试守护——若未来重构引入 `await` 到熔断器方法、或改变状态机推进逻辑，并发破坏不会被测试拦截（熔断器是核心保护组件，错乱会导致熔断失效或误熔断）。

### 根因

熔断器方法（`allow_request` / `record_success` / `record_failure` / `release_probe`）全部**同步无 await**——asyncio 单事件循环下每次方法调用原子执行（GIL，无 await 点不会协程切换），内存层面无数据竞争。因此「无锁安全」成立，但需要**测试守护**方法序列交错的语义正确性。

---

## 工业级参照

| 参照 | 做法 | 对应本项目 |
| --- | --- | --- |
| CPython asyncio 文档（同步原语） | 单线程但协程在 await 点交错，共享状态非自动安全；`Lock`/`Event`/`Semaphore` 用于协调 | 熔断器方法无 await → 单方法原子（无需锁）；测试验证序列交错 |
| [frontrun](https://pypi.org/project/frontrun/) | 确定性并发测试：DPOR 穷举交错 + 不变量校验；`reproduce_on_failure` 复现 | 轻量替代：用 `asyncio.Event` 构造确定性交错序列 |
| seedloop | asyncio 确定性模拟测试 | 同上 |

**核心**：同步方法在单线程原子执行，测试重点是**方法序列交错时的状态机语义**（探针 A 失败回 OPEN 后，探针 B 迟到 success 不被误关闭）。

---

## 修复方案（含决策取舍）

**决策**：在 `test_retry.py` 补充并发测试——`asyncio.gather` 多协程并发调用 + `asyncio.Event` 确定性交错控制，覆盖四条「无锁安全」不变量：

1. **并发 `allow_request` 槽位上限**：HALF_OPEN 下多协程并发放行，探针数不超过 `half_open_max_requests`；
2. **探针交错**：探针 A 失败 `record_failure` 回 OPEN 后，探针 B 迟到 `record_success` 被 no-op（不误关闭熔断器）；
3. **CLOSED 并发窗口统计**：并发 `record_*` 窗口记录无丢失（total == 调用次数）；
4. **4xx 释放槽位补位**：探针 4xx `release_probe` 后，后续正常请求能补位探测。

**取舍理由**：

1. 轻量（纯标准库 asyncio.Event），无需引入 frontrun/seedloop 外部依赖；
2. 确定性交错（Event 控制让出点）比随机时序更可靠、可复现（对齐 frontrun 的 `reproduce_on_failure` 思路）；
3. 覆盖子智能体审查指出的全部并发风险点（探针交错 / 槽位 / 窗口统计 / 4xx 竞争）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `tests/unit/test_retry.py` | 新增并发测试区 4 用例：并发放行槽位上限 / Event 探针交错（迟到 success no-op）/ 并发窗口记账无丢失 / 4xx 释放补位 | 新增 4 用例（`test_concurrent_*`） |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表加 LLM-005 条目） | — |

---

## 验证

- `tests/unit/test_retry.py` **41 passed**（含新增 4 条并发用例，验证「无锁安全」不变量全部成立）
- 全量测试 **357 passed**（44.26s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **「无锁安全」需要测试守护**：同步无 await 方法在 asyncio 单线程下原子执行，内存无竞争；但「方法序列交错」的语义正确性必须用并发测试验证——否则未来重构引入 await 即静默破坏。
- **确定性交错优于随机并发**：用 `asyncio.Event` 控制让出点构造确定性序列，比 `gather` 随机时序更可靠、可复现（对齐 frontrun DPOR + reproduce 思路）。
