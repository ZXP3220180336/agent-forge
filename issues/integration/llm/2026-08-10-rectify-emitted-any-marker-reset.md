# emitted_any 累积语义缺失：中断前最后一个 usage/finish-only chunk 把已产出标记冲成 False → 误整流

> **状态**：✅ 已修复（2026-08-10）
> **优先级**：P1（误整流 = 重复输出 + 双倍计费）
> **来源**：2026-08-10 修复 · 2026-08-16 从 llm.md 提取归档
> **涉及模块**：`app/integration/llm/streaming_rectifier.py`（整流判定 emitted_any）
> **关联文档**：[llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md)

---

## 问题描述

### 现象

`_apply_chunk` 返回「单 chunk 是否产出」而非累积——整流循环若直接用它覆盖 `emitted_any`，中断前最后一个 usage/finish-only chunk（该 chunk 无新 content，返回 False）会把**已产出**标记冲成 False。

### 影响

整流判定误判「未产出首 token」→ 对**已产出内容**的流执行整流重试（重新 create + 重新迭代）→ **重复输出 + 双倍计费**。

### 根因

整流循环入口 `emitted_any` 用 `_apply_chunk` 的单 chunk 返回值直接覆盖，未做累积（`emitted_any or chunk_emitted`）——中断边界上最后一个元数据 chunk 的 False 覆盖了此前 content 产出的 True。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 已产出标记必须单调 | 「首 token 是否已产出」是整流重试的**门槛信号**，一旦置 True 不可回退——累积语义（`a or b`），任何单 chunk 返回不能冲掉历史产出 |
| 元数据 chunk 不计产出 | usage/finish chunk 不是用户可见内容——不计产出，但也不能抹掉已产出状态 |

---

## 修复方案（含决策取舍）

**决策**：整流循环内 `emitted_any` 改为累积语义（`emitted_any = emitted_any or chunk_emitted`）。

**修复要点**：

1. `_apply_chunk` 仍返回「单 chunk 是否产出」（保持纯函数语义）；
2. **循环入口累积**：`emitted_any = emitted_any or chunk_emitted`——已产出标记单调递增，后续元数据 chunk 的 False 不再冲掉历史产出。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/streaming_rectifier.py` | 整流循环 `emitted_any` 累积语义 | `test_streaming_rectifier.py`——content 产出后遇 usage/finish-only chunk 中断，`emitted_any` 保持 True（不误整流） |

---

## 验证

- 已产出 content 后中断（最后一个 chunk 为 usage/finish-only）→ `emitted_any=True`，不整流——无重复输出/双倍计费
- refusal 纯拒绝流（无 content）仍可整流（`emitted_any=False`）
- 全量测试通过（2026-08-10 修复时验证）

---

## 教训沉淀

- **门槛信号必须单调**：「首 token 已产出」这类决定「是否可重试」的信号，一旦置位不可回退——用累积语义（`or`）而非覆盖，防止边界元数据 chunk 冲掉真实状态。
