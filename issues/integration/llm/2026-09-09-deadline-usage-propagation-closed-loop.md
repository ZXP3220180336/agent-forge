# LLM-047 流式 deadline 已获 usage 传播闭环不完整（部分出口漏传/误传）

> 状态：✅ 已修复 ｜ 优先级：P2（成本归账边界缺口，非功能故障） ｜ 发现：2026-09-09（usage 传递口径审查） ｜ 模块：streaming_rectifier / llm_service / errors / structured
> 关联：上承 [LLM-044](2026-09-08-execution-control-through-every-call.md)（执行终止信号双层 + 保留已获 usage）· [LLM-045](2026-09-09-execution-control-late-result-drop.md)（迟回值不丢）· [LLM-038](2026-09-02-usage-accounting.md)（usage 成本累计）

## 发现

流式整流 / 半流续接的各 `_DeadlineExceeded` 出口对「已获 usage」的传递口径不一致：

- **漏传（该带不带）**：整流退避**睡满后复查**命中 deadline 时裸抛（相邻「退避中被 deadline 中断」分支却重建携 usage）；半流续接退避被 deadline 中断时裸抛——裸抛直达 Facade `translate_abort` → `LLMDeadlineExceededError(usage=None)`，整流/续接中断流已读到的 usage chunk 不进入领域成本总账。
- **误传（不该带却带）**：整流/续接 attempt 入口（本轮 create 前）与续接 create/reserve 段（`_reset_dead_meta` 刚清空、`continue_fn` 不读流）防御性携带 `result.usage`——该点恒为 None，纯属噪音，且掩盖「termination 出口是否真可能携 usage」的判据。

影响：deadline 恰好命中在「已收 usage chunk 之后」的窗口时成本系统性低估；收尾出口口径无单一判据可维护。

## 分析

1. **usage 唯一来源**：流式 `result.usage` 只在 `_apply_chunk` 收到 usage-only chunk 时填充，与 content 时序解耦（usage 不算「首 token」，整流失败流可能已收 usage——`test_rectifies_pre_first_token_interrupt` 证实用例）；唯一清空点是整流 continue 前与续接 create 前的 `_reset_dead_meta`。
2. **判据**：`_DeadlineExceeded.usage` 语义 =「期限耗尽前已完成真实调用的用量」（LLM-044 docstring「保留已获 usage」）。凡出口路径上 `result.usage` **可能非空**（流已读、放弃/续接链、整流失败后）→ 必须携带；凡**可证明恒 None**（请求未发/未读流：attempt 入口、create/reserve 段）→ 裸抛，与 create 段一致。原语层（`execution_control` 三处）无从取 usage，一律裸抛，由最近的读取捕获点统一补全——该分层正确，问题只在整流/续接语义层个别出口未按判据执行。
3. **漏传根因**：整流退避的「被中断」与「睡满复查」是同一退避、同一 usage 来源，却一个重建一个裸抛；续接退避沿用了裸抛习惯，未镜像整流退避的 except 补全。

## 工业级参照

- 流式 completion 的 usage 随独立 chunk 于收尾前到达（无 choices、仅 usage），可达时点与 deadline 命中无顺序保证 → 终止出口必须以「已累积 usage」而非「是否到流尾」为准携带（本仓库实测结构见 `_usage_chunk` 用例，即参照形态）。
- 成本护栏以 usage 为唯一计量依据（LLM-038 既定口径）：终止路径丢 usage 即护栏低估，与重试/降级不累计同源。

## 修复

代码（本次已落地，随 LLM-044 所在分支工作区）：

1. **整流退避睡满复查** → `raise _DeadlineExceeded(usage=result.usage)`，与「被中断」分支同源同值，统一携带。
2. **半流续接退避**补 `except _DeadlineExceeded` → 重建携 usage（镜像整流退避；usage 非空即带、None 时等价裸抛）。
3. **attempt 入口** → 裸抛（本轮 create 前无已耗用量）。
4. **续接 create/reserve 段** → 原样 re-raise（`_reset_dead_meta` 已清 + 未读流，恒 None）。
5. 顺带同点补全：续接退避**被 `_StreamCancel` 中断**走用户取消出口（error 事件 + `outcome.completed`），不再裸抛丢 SSE 取消事件。

## 实施记录

| 文件 | 改动 |
| --- | --- |
| `app/integration/llm/streaming_rectifier.py` | 上述 4 处 deadline 出口 usage 收紧 + 续接退避取消出口；attempt 入口裸抛注释 |
| `tests/unit/test_streaming_rectifier.py` | 新增整流退避 deadline 保留 usage；续接退避 deadline 保留 usage（增强）；续接退避 cancel 走取消出口 |
| `tests/unit/test_llm_request_budget.py` | 新增 async_generate 整流退避 deadline usage 跨 Facade 不丢（LLM-047 #4）；非流式迟回测试参数化 cancel/deadline + usage 断言（LLM-045 扩展，LLM-047 #5） |
| `tests/unit/test_generate_structured.py` | 新增携 usage 终止（cancel/deadline 参数化）累计 + 停止降级断言（LLM-047 #6） |

## 验证

### 已闭环（LLM-047 验收）

1. ✅ ~~追踪优化 deadline 出口 usage 传递~~：整流退避睡满复查补携 usage、续接退避 deadline/cancel 出口补全、attempt 入口与续接 create 段收紧为裸抛（见上「修复」）。
2. ✅ ~~整流退避 deadline 保留 usage 测试~~：`test_rectify_backoff_deadline_preserves_usage`。
3. ✅ ~~半流续接退避 deadline 保留 usage 测试~~：`test_continuation_backoff_obeys_deadline_and_never_calls_provider`（整流中断流携 usage）+ `test_continuation_backoff_cancel_goes_cancel_exit`。
4. ✅ **async_generate Facade 跨层**：`test_llm047_stream_rectify_deadline_usage_survives_facade_translation`（经 `LLMService.async_generate` 整链直测：整流退避段 deadline 命中 → `translate_abort` 后 `LLMDeadlineExceededError.usage` 不丢；不再发起第二次 create）。
5. ✅ **非流式迟回测试参数化**：`test_llm045_nonstream_create_swallows_abort_settles_actual_then_aborts_usage_kept` 参数化 cancel / deadline 两分支，锁定终止异常 `usage == sr.usage` 且 `settle(actual usage)`。
6. ✅ **structured 断言**：`test_typed_abort_with_usage_accumulates_and_stops_degrade`（cancel/deadline 参数化）——`_call_generate` 对携 usage 终止 `_accumulate_usage` 累计后 `raise`，extract 最外层收敛 None、剩余降级级不再发起。

### 跨层测试的边界设计（维护备忘）

- 整流 attempt0 整链前置（预算 tiktoken 估算 / reserve / create / 读首流）会吃掉极小 deadline → 若 deadline 过短会先命中 attempt 入口裸抛（该点确无已读 usage，为判据内语义），测不到整流退避段。跨层测试须给 deadline 留缓冲（本测试 0.5s）且固定整流退避参数（1s、无抖动）确保命中退避段。
- 整流 attempt≥1 的 create/reserve 段 deadline（`rectified_stream` L442 re-raise）：上一整流 attempt 的 usage 已被整流 continue 前 `_reset_dead_meta` 清空——该出口恒 None、裸抛为判据内语义；上一 attempt 已读 usage 只在整流退避段（reset 前）携带。勿在 create 段补 usage（会是伪造的 None 噪声）。

### 文档收尾

- 执行控制 / 整流 / llm_service 模块文档核对：llm_service.md 检查点 3「结算后复查命中携 usage 抛 shared 终止」等描述与本 issue 行为一致；整流/续接**退避出口级** usage 携带属内部行为、模块文档未细化到该粒度，对外契约不变 → **模块文档无需改动**。
- issues 索引、todo 交接已同步；lessons 教训见 [lessons.md](../../../docs/lessons.md) LLM-047 节。

全量 `uv run pytest`（900 passed）与 `uv run python -m scripts.verify_alignment`（通过）验证完毕。

## 教训

- **usage 传递判据 = 「该出口是否可能携已获用量」，而非「统一带或统一裸」**：流已读 / 已结算的出口必须带（漏即成本低估）；请求未发的出口不带（防御性携带是噪音、并模糊判据）。原语层（无 usage 来源）裸抛、语义层（知道 result.usage）按上述判据携带——补全点单一。
- **同段代码的对称出口必须同一口径**：「退避被中断」与「退避睡满复查」共享同一 usage 来源，一个带一个裸即缺陷；修复一个出口时镜像检查其对称出口。
