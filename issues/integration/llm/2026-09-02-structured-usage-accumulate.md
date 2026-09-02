# generate_structured usage 回填不累计 + 变量错位，成本护栏系统性低估

> **状态**：✅ 已修复（2026-09-02）
> **优先级**：P1（成本护栏失效：reflection 的 cost_limiter.check 依据回填 usage 折算成本，低估会让实际超预算不触发停机）
> **来源**：2026-09-02 产物完整性审计发现（LLM 传输层 → 领域层产物梳理）
> **涉及模块**：`app/integration/llm/structured.py`（`StructuredOutput.extract` / `_try_extract` / `_fallback_extract` / `_call_generate`）· 消费方 `app/domain/reasoning/reflection.py`
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [ports.md](../../../docs/domain_doc/ports_doc/ports.md)

---

## 问题描述

### 现象

`generate_structured(..., usage=usage)` 的 `usage` 可变参数回填有两重缺陷：

1. **不累计**：三级降级过程中，前置失败级 / 截断重试 / 回喂重试的真实 token 消耗全部丢失，`usage` 只反映最后一次成功调用——extract 全程消耗被系统性低估。
2. **变量错位**：回喂修正成功后，回填的是 `result.usage`（首次调用），而非 `retry.usage`（回喂调用，messages 更长、prompt_tokens 更大）——回喂修正成功的那次调用消耗被丢弃。

### 影响

消费链路：`app/domain/reasoning/reflection.py` 用回填 usage 经 `_merge_usage` 累计 `_structured_usage` → 循环顶部 `cost_limiter.check`（成本超限停机降级）与 `ReflectionOutcome.total_tokens/usage` 统计。

- **成本护栏低估**：回填缺失让 `check` 折算成本偏低，实际花费超预算可能不触发停机降级（产品侧直接损害）；
- **统计失真**：`outcome.total_tokens / usage` 低于真实消耗，评测 / 审计 / 计量失真。

### 根因

结构化提取是一个**多次真实 LLM 调用**的编排（三级降级 × 每级内截断重试 / 回喂重试），而 usage 回填只挂在「最后一次解析成功」的单点上，且引用了错误的 StreamResult 变量——没有在每次调用的统一出口累计。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI Agents SDK / Vercel AI SDK | 成本计量面向**会话全程真实消耗**累计（重试 / 降级均计入），非「最后一条成功消息」 |
| 成本护栏设计原则 | 护栏（cost ceiling）须基于真实累计消耗判定，低估即护栏失效——宁可高估触发保护，不可低估放行 |

**核心**：`usage` 是「extract 全程消耗」的计量载体（供成本护栏），须在每次真实成功调用的**统一出口**累加，而非在成功返回点回填单次。

---

## 修复方案（含决策取舍）

**决策**：usage 累加**单一归口到 `_call_generate`**——它是 extract 内所有真实模型调用的唯一出口（多级降级 / 截断重试 / 回喂全经过它）。

1. `_call_generate` 新增 `usage` 参数，每次拿到带 usage 的成功响应即累加进目标 dict（新增模块级 `_accumulate_usage`：逐 key 相加，嵌套明细无可累加语义则保留最新）；
2. 删除 `_try_extract` / `_fallback_extract` 内 4 处散落的 `usage.update(...)`——既消除「覆盖非累加」，也消除「回喂成功回填 `result` 而非 `retry`」的变量错位（累加发生在调用返回当刻，变量必然正确）；
3. `_try_extract` 的 `_call_generate`（首调 / 截断重试 / 回喂）与 `_fallback_extract` 的 `_call_generate` 均透传 `usage=usage`；
4. 语义变更同步 docstring：`extract` / `generate_structured` 的 usage 描述从「回填本次成功调用」改为「回填全程累计（含降级/截断重试/回喂的所有成功调用，供成本计量）」。
5. **辅助函数守卫修正**：`_accumulate_usage` 判空须用 `target is None` 而非 `not target`——空 dict `{}` 是合法累加起点（首个调用即触发），`not target` 会静默跳过累加。

**取舍**：`generate` 返回 None（下游失败，无可计量响应）的调用不计入——数据不可得，非本次范围；不计"失败调用的 token"（与反射 / 非流式重试口径一致）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | 新增 `_accumulate_usage`（逐 key 累加）；`_call_generate` 增 `usage` 参数 + 成功响应累加；删除 4 处散落 `usage.update`；`_try_extract` / `_fallback_extract` 各 `_call_generate` 调用透传 `usage`；extract usage docstring 更新 | `tests/unit/test_generate_structured.py` 新增 1 例（降级 + 回喂后 usage = 4 次成功调用全量累加） |
| `app/integration/llm/llm_service.py` | `generate_structured` usage docstring 更新为全程累计语义 | — |
| `docs/integration_doc/llm_doc/llm.md` | `generate_structured` 契约行：usage 改为「全程累计」 | — |
| `docs/integration_doc/llm_doc/structure.md` | 对外接口行：usage 改为「全程累计」；问题记录清单加本条目 | — |
| `docs/domain_doc/ports_doc/ports.md` | 端口契约行：usage 改为「全程累计」 | — |

---

## 验证

- 新增回归测试 `test_usage_accumulates_across_degrade_and_reask`：第一级 + 回喂 ×2 均失败 → 第二级成功，4 次成功调用 usage 全量累加（`{prompt_tokens:100, completion_tokens:10, total_tokens:110}`）——修复前为空 / 只含末次，锚定新语义；
- `tests/unit/test_generate_structured.py` 50 passed（含新例）；
- `tests/unit/test_reflection.py` + `test_reflection_agent.py` 35 passed（消费方无回归）；
- `uv run python -m scripts.verify_alignment` 通过。

---

## 教训沉淀

- **成本计量 = 全程真实消耗，护栏才成立**：凡「多次真实调用 + 一次返回」的编排（降级链 / 重试 / 回喂），用量回填必须在**每次调用的统一出口累加**，不能挂在成功返回点回填单次——低估即护栏失效；
- **回填须取自「产出该结果的那次调用」**：跨循环引用初始变量（`result`）而非最新调用（`retry`）是隐蔽的变量错位，只在「回喂后成功」路径触发、无单测锚定则永不复现——新增行为必须带回归护栏；
- **空 dict 是合法累加起点**：累加辅助函数的判空守卫须区分 `None` 与 `{}`（`not target` 会把首个调用静默吞掉）。
