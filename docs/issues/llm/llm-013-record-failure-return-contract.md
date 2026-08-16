# LLM-013 record_failure() bool 返回值契约无人兑现，retry.md 误导

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P2（文档-实现偏差）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 12）
> **涉及模块**：`app/integration/llm/retry.py`（`CircuitBreaker.record_failure`）· `docs/integration_doc/llm_doc/retry.md`
> **关联文档**：[retry.md](../../integration_doc/llm_doc/retry.md)

---

## 问题描述

### 现象

`record_failure()` docstring 声明「Returns True 表示本次失败将熔断器切换到 OPEN——调用方可据此感知熔断已触发」，但**全部 7 处调用**（retry.py 内 6 处 + streaming_rectifier.py 1 处）都忽略返回值。而 retry.md 改造记录「问题 2」宣称「`record_failure()` 返回 bool，触发 OPEN 时立即 `break`」——当前实现是**请求级记账**（重试循环结束后统一记录一次），单请求内熔断评估从不打断剩余重试。

### 影响

维护者按 retry.md 预期「熔断可提前中断请求」会得到错误结论——实际单请求内熔断不打断重试（这是**有意设计**：请求级粒度避免单请求重试放大窗口统计）。

### 根因

bool 返回值契约无人兑现 + retry.md「问题 2」改造记录与实况不符（文档误导）。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 本项目请求级记账设计 | 一次 `execute()` 只向窗口汇报一次（成功循环内、失败耗尽后统一）——单请求内熔断评估不打断剩余重试（避免重试放大） |
| 代码契约实践 | 无人消费的返回值应明确标注（作语义标记）或移除——文档不得宣称实际不存在的行为 |

**核心**：返回值保留（语义标记）或移除，但文档必须与实现一致——「立即 break」是误导。

---

## 修复方案（含决策取舍）

**决策**：**保留 bool 返回值作语义标记**，澄清 docstring（「当前无调用方消费，供未来请求级/半开优化使用」）；修正 retry.md「问题 2」为请求级记账语义（重试循环结束后统一记录，单请求内不打断）。

**取舍理由**：

1. 删 bool（改 None）需动 7 处调用签名 + 测试断言，且丢失未来可用的语义标记（半开/请求级优化可能消费）；
2. 保留 + 文档对齐是最小正确修复——返回值无害，误导在文档；
3. 请求级记账是有意设计（LLM-003 重试粒度），「不打断剩余重试」是正确行为，文档应如实描述。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/retry.py` | `record_failure` docstring 澄清：返回值保留作语义标记，当前无调用方消费（请求级记账，单请求内不打断） | 无（行为不变） |
| `docs/integration_doc/llm_doc/retry.md` | 「问题 2」条目修正为请求级记账语义（`record_failure` 返回 bool 但「立即 break」是误导） | — |
| `docs/integration_doc/llm_doc/llm.md` | 已实现列表加 LLM-013 条目 | — |

---

## 验证

- 全量测试 **364 passed**（仅 docstring + 文档变更，回归确认）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过---

## 教训沉淀

- **无人消费的返回值要么移除、要么明确标注**：`record_failure()` 返回 bool 但 7 处调用全忽略——契约保持但不兑现会误导维护者（以为可提前中断）。
- **文档不得宣称不存在的行为**：retry.md「触发 OPEN 立即 break」与请求级记账实况不符——改造记录应如实描述当前语义（或标注已变更）。
