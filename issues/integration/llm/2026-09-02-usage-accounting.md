# LLM usage 计量口径问题追踪（LLM-038 已修复 / LLM-039 口径记录）

> 本文件追踪 LLM 传输层「哪些 token 消耗计入 usage」的两个相邻问题——两者构成 usage
> 计量口径的完整边界：**缺陷1（LLM-038）**是数据可得的语义层浪费未累计（已修复）；
> **缺陷3（LLM-039）**是数据不可得的传输层失败消耗（记录为口径，不修，含升级路径）。
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [ports.md](../../../docs/domain_doc/ports_doc/ports.md)

---

## 缺陷1（LLM-038）generate_structured usage 回填不累计 + 变量错位，成本护栏系统性低估

> **状态**：✅ 已修复（2026-09-02）
> **优先级**：P1（成本护栏失效：reflection 的 cost_limiter.check 依据回填 usage 折算成本，低估会让实际超预算不触发停机）
> **来源**：2026-09-02 产物完整性审计发现（LLM 传输层 → 领域层产物梳理）
> **涉及模块**：`app/integration/llm/structured.py`（`StructuredOutput.extract` / `_try_extract` / `_fallback_extract` / `_call_generate`）· 消费方 `app/domain/reasoning/reflection.py`

### 问题描述

#### 现象

`generate_structured(..., usage=usage)` 的 `usage` 可变参数回填有两重缺陷：

1. **不累计**：三级降级过程中，前置失败级 / 截断重试 / 回喂重试的真实 token 消耗全部丢失，`usage` 只反映最后一次成功调用——extract 全程消耗被系统性低估。
2. **变量错位**：回喂修正成功后，回填的是 `result.usage`（首次调用），而非 `retry.usage`（回喂调用，messages 更长、prompt_tokens 更大）——回喂修正成功的那次调用消耗被丢弃。

#### 影响

消费链路：`app/domain/reasoning/reflection.py` 用回填 usage 经 `_merge_usage` 累计 `_structured_usage` → 循环顶部 `cost_limiter.check`（成本超限停机降级）与 `ReflectionOutcome.total_tokens/usage` 统计。

- **成本护栏低估**：回填缺失让 `check` 折算成本偏低，实际花费超预算可能不触发停机降级（产品侧直接损害）；
- **统计失真**：`outcome.total_tokens / usage` 低于真实消耗，评测 / 审计 / 计量失真。

#### 根因

结构化提取是一个**多次真实 LLM 调用**的编排（三级降级 × 每级内截断重试 / 回喂重试），而 usage 回填只挂在「最后一次解析成功」的单点上，且引用了错误的 StreamResult 变量——没有在每次调用的统一出口累计。

### 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI Agents SDK / Vercel AI SDK | 成本计量面向**会话全程真实消耗**累计（重试 / 降级均计入），非「最后一条成功消息」 |
| 成本护栏设计原则 | 护栏（cost ceiling）须基于真实累计消耗判定，低估即护栏失效——宁可高估触发保护，不可低估放行 |

**核心**：`usage` 是「extract 全程消耗」的计量载体（供成本护栏），须在每次真实成功调用的**统一出口**累加，而非在成功返回点回填单次。

### 修复方案（含决策取舍）

**决策**：usage 累加**单一归口到 `_call_generate`**——它是 extract 内所有真实模型调用的唯一出口（多级降级 / 截断重试 / 回喂全经过它）。

1. `_call_generate` 新增 `usage` 参数，每次拿到带 usage 的成功响应即累加进目标 dict（新增模块级 `_accumulate_usage`：逐 key 相加，嵌套明细无可累加语义则保留最新）；
2. 删除 `_try_extract` / `_fallback_extract` 内 4 处散落的 `usage.update(...)`——既消除「覆盖非累加」，也消除「回喂成功回填 `result` 而非 `retry`」的变量错位（累加发生在调用返回当刻，变量必然正确）；
3. `_try_extract` 的 `_call_generate`（首调 / 截断重试 / 回喂）与 `_fallback_extract` 的 `_call_generate` 均透传 `usage=usage`；
4. 语义变更同步 docstring：`extract` / `generate_structured` 的 usage 描述从「回填本次成功调用」改为「回填全程累计（含降级/截断重试/回喂的所有成功调用，供成本计量）」。
5. **辅助函数守卫修正**：`_accumulate_usage` 判空须用 `target is None` 而非 `not target`——空 dict `{}` 是合法累加起点（首个调用即触发），`not target` 会静默跳过累加。

**取舍**：`generate` 返回 None（下游失败，无可计量响应）的调用不计入——**该场景属缺陷3（传输层失败消耗不可得），非本次范围**；两类边界在本文件「缺陷1 与缺陷3 的分界」详述。

### 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | 新增 `_accumulate_usage`（逐 key 累加）；`_call_generate` 增 `usage` 参数 + 成功响应累加；删除 4 处散落 `usage.update`；`_try_extract` / `_fallback_extract` 各 `_call_generate` 调用透传 `usage`；extract usage docstring 更新 | `tests/unit/test_generate_structured.py` 新增 1 例（降级 + 回喂后 usage = 4 次成功调用全量累加） |
| `app/integration/llm/llm_service.py` | `generate_structured` usage docstring 更新为全程累计语义 | — |
| `docs/integration_doc/llm_doc/llm.md` | `generate_structured` 契约行：usage 改为「全程累计」 | — |
| `docs/integration_doc/llm_doc/structure.md` | 对外接口行：usage 改为「全程累计」；问题记录清单加本条目 | — |
| `docs/domain_doc/ports_doc/ports.md` | 端口契约行：usage 改为「全程累计」 | — |

### 验证

- 新增回归测试 `test_usage_accumulates_across_degrade_and_reask`：第一级 + 回喂 ×2 均失败 → 第二级成功，4 次成功调用 usage 全量累加（`{prompt_tokens:100, completion_tokens:10, total_tokens:110}`）——修复前为空 / 只含末次，锚定新语义；
- `tests/unit/test_generate_structured.py` 50 passed（含新例）；
- `tests/unit/test_reflection.py` + `test_reflection_agent.py` 35 passed（消费方无回归）；
- `uv run python -m scripts.verify_alignment` 通过。

---

## 缺陷3（LLM-039）传输层失败调用的 token 消耗不可计量（数据不可得）

> **状态**：🔵 口径记录（2026-09-02 评估定案；数据不可得，不修）
> **优先级**：P3（影响 = 成本统计低估 + 成本护栏轻微延后，低估量有界且有兜底方向）
> **来源**：2026-09-02 产物完整性审计发现；整流重试 usage 清除疑议（评审追问）触发深入分析，与缺陷1 合档
> **涉及模块**：`app/integration/llm/llm_service.py`（非流式 generate）/ `streaming_rectifier.py`（整流）/ `retry.py`（create 重试 / fallback）/ `structured.py`（extract）——跨全部真实调用路径
> **关联文档**：[llm_gateway.py](../../../app/domain/ports/llm_gateway.py)（usage 口径注释）· 本文档缺陷1 节（分界）

### 覆盖范围与失败形态

#### 覆盖路径

传输层**失败/放弃的尝试已消耗真实 token（prompt 通常已计费），但 usage 字段不含这些消耗**——`StreamResult.usage`（流式/非流式）与 `generate_structured` 的 usage 回填，都只反映「最终成功响应」的用量。具体覆盖路径：

| 路径 | 失败形态 | 成功尝试的 usage 是否计入 | 失败尝试消耗 |
| --- | --- | --- | --- |
| create 阶段 retry | 429 / 5xx / 超时重试耗尽 | 计入（最后一次成功） | **不可得**（异常无 usage） |
| 流式迭代整流 | 首 token 前中断后重试 | 计入（整流成功那次） | **不可得**（usage chunk 在流末尾，中断在其前） |
| fallback | 主调用全失败 → 备用模型成功 | 计入（fallback 响应） | **不可得**（主调用侧无响应体） |
| 结构化降级（缺陷1 范围） | 调用成功但内容不可用 | **已累计**（缺陷1 修复后） | 数据可得 → 已修 |

#### 影响面（量级）

低估源 = 失败尝试数 × 单次 prompt 大小。在 Agent 场景 prompt 可能很大（携带多轮工具结果 / 证据链，几十 K token）：

- 连续 3 次失败（`max_retries`=2 + fallback）+ 大上下文 ≈ 数万 token 未计入；
- **方向性**：对成本护栏（停机闸）是「低估延后停机」，且低估恰好集中在「连续失败正要停机」的当口；对成本统计是「记录 < 真实账单」。

### 与缺陷1 的分界：数据可得性

「浪费的 token 没计入」分两种，处理方式完全不同：

| | 缺陷1（LLM-038，已修） | 缺陷3（LLM-039，口径） |
| --- | --- | --- |
| 失败形态 | **语义层失败**：调用成功返回（带 usage），但内容判不可用 → 触发降级 / 回喂 | **传输层失败**：create 抛异常 / 流中断 / 整流放弃 / fallback 主调用失败 |
| usage 数据 | **可得**（每次成功响应都带 usage） | **不可得**（异常 / 中断不返回 usage） |
| 处置 | 累加所有成功调用（修复） | 无数据可累加，记录为口径不修 |

缺陷1 修复后，结构化路径「输出不合规但已计费」的消耗已计入；**剩下没计入的只剩传输层失败——即缺陷3。** 二者不是遗漏程度差异，是**数据可得性**差异。

### 数据面根因

服务商按「请求实际发出的 token」记账，但 **usage 字段只随成功响应体返回**：

- create 阶段失败 → 抛异常，异常对象无 usage；
- 迭代阶段中断 → usage chunk 在**流末尾**，中断发生在它到达之前；
- fallback 场景 → 主调用消耗在主调用侧，响应体在 fallback 侧。

客户端从 API 侧**拿不到**失败尝试的准确 token 数——协议层面的信息缺失，非代码漏记。唯一「部分可得」窗口：整流前死流恰好送达 usage chunk 才报错（≈ 流已完整结束却在最后抛错），近不可达。

### 工业级参照（失败消耗计量的业界惯例）

| 参照 | 做法 |
| --- | --- |
| OpenAI SDK / LangChain / LangGraph | usage 即最后一次成功响应的用量，失败尝试一律不回溯（也回溯不了） |
| LangSmith / LiteLLM / Helicone 等成本追踪器 | 成功调用按响应 usage 记账（可审计）；失败调用记**独立事件**（tiktoken 估算 prompt），报表分「精确消耗 + 估算消耗」两列；**成本护栏只用精确列** |

**核心**：usage 字段保持「成功响应的真实用量」、不掺估算——混入估算会让下游成本报告失去可审计性（对不上服务商账单），且估算高估会反向触发误停机。

### 决策与取舍

**整流路径 usage 清除是缺陷3 的一个实例（评审结论）**：

`streaming_rectifier` 整流重试 `continue` 前清空 `result.usage`，曾被质疑「清除造成本轮消耗失真」。分析后确认清除正确：

1. 整流成功的新流以 `stream_options={"include_usage": True}` 发起，末尾 usage chunk **必然覆盖** result.usage——清与不清最终值相同；
2. 死流中断发生在 usage chunk 到达之前，`result.usage` 此刻本就是 None，清除是清了个空；
3. 真正必须清的是 refusal（死流元数据不残留、防下游误判拒答）——usage 的清除是同一意图的显式表达；
4. 死流消耗不可得 → 归缺陷3 口径，与 create 重试 / fallback 主调用失败同一处置。

**不修的理由**：

1. 数据不可得——无法从 API 拿到失败尝试的准确 token；
2. 字段语义约束——usage 被 react 逐轮累加、settle 退差、cost_tracker 折算全链路当作「单次成功响应用量」；混入估算会使字段失去可审计性并可能高估误停机；
3. 修不全——「死流有 usage 才可得」的边缘即使修也留一个「有时累计有时不累计」的怪异口径。

**不引入的替代方案**（若保留累加会撞的坑）：

- usage 字段语义漂移为「含估算的累计」→ 下游 react / settle / cost_tracker 全链路口径需改；
- 估算 ≠ 计费：死流消耗只能按 `_count_prompt_tokens` 上限估算，报表对不上账单。

### 升级路径（未来可选）

1. **usage 契约口径文档化**（本次已落地）：`StreamResult.usage` 注释标注「= 成功响应用量，不含失败/放弃/重试/fallback 主调用的消耗」——见 [llm_gateway.py](../../../app/domain/ports/llm_gateway.py)；
2. **失败估算消耗放观测侧**：若未来做成本报表，失败尝试的估算 token 作为独立事件上报（与 usage 分离），不改变 usage 语义。

### 结论

- 缺陷3 定性为**数据不可得 + 字段语义约束 → 口径记录，非待修 bug**；
- 与缺陷1 的本质区别：缺陷1 数据可得却不累计是实现缺陷（已修）；缺陷3 数据不可得，修不动也不该硬修；
- 整流 usage 清除只是缺陷3 在流式路径的一个实例，论证后不改是正确取舍。

---

## 教训沉淀

- **成本计量 = 全程真实消耗，护栏才成立**（缺陷1）：凡「多次真实调用 + 一次返回」的编排，用量回填必须在**每次调用的统一出口累加**，不能挂在成功返回点回填单次——低估即护栏失效；
- **回填须取自「产出该结果的那次调用」**（缺陷1）：跨循环引用初始变量（`result`）而非最新调用（`retry`）是隐蔽变量错位，无单测锚定则永不复现——新增行为必须带回归护栏；
- **空 dict 是合法累加起点**（缺陷1）：累加辅助函数的判空守卫须区分 `None` 与 `{}`；
- **「浪费 token 没计入」须先分「可得 / 不可得」再谈修**（缺陷3 分界）：语义层失败数据可得 → 是缺陷要修；传输层失败数据不可得 → 是口径要记录。把不可得的当缺陷修，要么修不了要么引入估算污染可审计性。
