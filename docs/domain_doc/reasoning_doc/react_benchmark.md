# ReAct 策略工业级对标（完成度基准）

> **对象**：`app/domain/reasoning/react.py`（ReActStrategy）
> **更新日期**：2026-08-27
> **定位**：ReAct 推理策略的工业级能力基准与差距清单——供策略增强、Reflection / CoT 实现参考
> **依据**：LangChain / LangGraph / OpenAI Agents SDK / SMOLagents / Claude Agent SDK 源码级调研（2026-08-27）

---

## 📋 目录

- [ReAct 策略工业级对标（完成度基准）](#react-策略工业级对标完成度基准)
  - [📋 目录](#-目录)
  - [结论先行](#结论先行)
  - [工业级参照：ReAct 工具循环能力清单](#工业级参照react-工具循环能力清单)
    - [核心必备（13 项）—— 缺失会明显损害主链路正确性 / 可观测性 / 稳定性](#核心必备13-项-缺失会明显损害主链路正确性--可观测性--稳定性)
    - [增强项（10 项）—— 锦上添花或特定场景（HITL / 多 Agent / 长任务）才需要](#增强项10-项-锦上添花或特定场景hitl--多-agent--长任务才需要)
  - [本项目逐项对照](#本项目逐项对照)
    - [核心必备对照](#核心必备对照)
    - [增强项对照](#增强项对照)
  - [发现的问题（按优先级）](#发现的问题按优先级)
  - [产品导向视角](#产品导向视角)
  - [修复优先级建议](#修复优先级建议)
  - [相关文档](#相关文档)

---

## 结论先行

- **核心循环结构完成度 100%**：主循环 / 终止判定 / 超限降级 / LLM 错误分工 / 工具并行 / 事件流 / 总时间上限 / 工具失败回喂 / 解析降级 / 上下文预算全部到位。
- **核心必备 13 项全部完备**（工业级核心模式 13/13 落地）。
- **增强项进展**：结构化输出约束（#15）已落地（Final Answer 工具）；其余增强项按「不做或预留」原则（见产品导向视角）。
- **架构加分**：策略模式解耦（`reasoning → ports + shared`）、端口抽象、独立测试，模块划分优于多数工业级框架。

---

## 工业级参照：ReAct 工具循环能力清单

> 工业级 ReAct 循环不是「LLM 调用 + 工具执行」两行代码，而是围绕下述维度的工程护栏。四大框架的共同主线：**错误一律转文本回喂模型自愈，框架只负责分类（哪些可重试、哪些要短路）**，而非框架自己猜答案。

### 核心必备（13 项）—— 缺失会明显损害主链路正确性 / 可观测性 / 稳定性

| # | 特性 | 工业级普遍做法 | 代表实现 |
| --- | --- | --- | --- |
| 1 | 迭代上限 | `max_iterations/max_turns/max_steps`，默认 10–64 | OpenAI 10 / LangChain 15 / SMOL 20 / Claude 20–64 |
| 2 | 时间上限 | `max_execution_time`（同步检查 + 异步 `asyncio.timeout`） | LangChain AgentExecutor |
| 3 | 正常终止判定 | 无 tool_call / 输出满足 final 类型 / 结束标记（`Finish[...]` / `TERMINATE` / `final_answer` / `end_turn`） | 全部框架 |
| 4 | 超限降级 | 不抛裸异常：常量提示（force）/ 多调一次 LLM 提取答案（generate） | LangChain `early_stopping_method` |
| 5 | 工具异常回喂 | 捕获 → 转文本作为 observation/tool_result 回喂 → 模型自愈 | OpenAI `failure_error_function` / SMOL 固定句式回喂 |
| 6 | 无效工具名处理 | 生成「工具不存在，请重试」observation 回喂 | LangChain `InvalidTool` / OpenAI `tool_not_found` |
| 7 | 解析 / JSON 失败降级 | `handle_parsing_errors` / schema 校验失败转错误文本回喂 | LangChain / SMOL structured |
| 8 | LLM 调用错误分类重试 | 网络/限流 → 指数退避 + jitter；取消/拒答 → 短路不重试 | OpenAI（0.25s→2s×2）/ SMOL（60s×2^n，仅限流） |
| 9 | 工具结果回喂 | observation / tool_result 追加进历史，下轮带入 | 全部框架 |
| 10 | 上下文预算管理 | trim 最近 N 步 / 工具输出截断（SMOL 50KB）/ compaction | LangChain / SMOL / OpenAI |
| 11 | 事件 / 回调体系 | on_tool_start/end、on_llm_start/end、on_agent_finish | 全部框架 |
| 12 | 步数 / token 统计 | usage 聚合、step 计数、`intermediate_steps` 轨迹 | LangChain / OpenAI |
| 13 | 流式输出 | 逐步 yield 步骤 + token 流；思考与答案分开事件 | OpenAI / Claude（thinking vs text 独立 block） |

### 增强项（10 项）—— 锦上添花或特定场景（HITL / 多 Agent / 长任务）才需要

| # | 特性 | 工业级普遍做法 | 代表实现 |
| --- | --- | --- | --- |
| 14 | 并行工具执行 | 默认并行 + 并发上限；多工具失败优先级仲裁 | OpenAI / Claude 并行 tool_use |
| 15 | 结构化输出约束 | strict JSON schema 约束中间推理（SMOL `{thought,code}`）或最终答案（OpenAI output_type） | SMOL / LangGraph |
| 16 | 输入 / 输出 guardrail | 输入 guardrail 与首轮并行、输出 guardrail 校验 final、tripwire 中断 | OpenAI |
| 17 | 权限 / 审批（HITL） | interrupt_before/after、approval item、permission prompt、白名单 | LangGraph / Claude / OpenAI |
| 18 | 自动摘要 / compaction | 超预算触发摘要（`context_management.budget=30%`） | Claude Agent SDK |
| 19 | 断点续跑 / 持久化 | checkpointer / session store / 状态恢复 | LangGraph |
| 20 | 成本上限 | token / cost ceiling 自动停机 | Claude Agent SDK |
| 21 | 沙箱 / 安全执行 | AST 解释器、import 白名单、远程 sandbox（e2b/docker/wasm） | SMOL CodeAgent |
| 22 | 最终答案校验 | `final_answer_checks` 断言后才接受 | SMOL |
| 23 | 错误处理策略可扩展 | error_handlers 按类型注册（max_turns / invalid_final_output / model_refusal） | OpenAI |

---

## 本项目逐项对照

> 状态标注：✅ 完备 ｜ ⚠️ 部分 / 有隐患 ｜ ❌ 缺失。

### 核心必备对照

| # | 特性 | 状态 | 本项目实现 |
| --- | --- | --- | --- |
| 1 | 迭代上限 | ✅ | `max_iterations`（默认 10），`for` 循环 + 超限兜底（[react.py:119](../../../app/domain/reasoning/react.py#L119)），量级与工业级一致 |
| 2 | 时间上限 | ✅ | `asyncio.timeout` 包整个循环实现总时长上限（[react.py:118](../../../app/domain/reasoning/react.py#L118)），超时对齐 `max_iterations` 兜底降级（[react.py:236](../../../app/domain/reasoning/react.py#L236)）；生产值由 `agent_timeout=300` 注入 |
| 3 | 正常终止判定 | ✅ | `finish_reason` 分支（tool_calls / stop / length），OpenAI 协议直接判定，比 LangChain 正则解析 `Finish[...]` 更稳（[react.py:181](../../../app/domain/reasoning/react.py#L181)） |
| 4 | 超限降级 | ✅ | 迭代超限用 `last_result` 兜底，无结果则 `error="LLM 未返回任何结果"`——等价 LangChain `force` 语义，不抛裸异常（[react.py:214-234](../../../app/domain/reasoning/react.py#L214-L234)） |
| 5 | 工具异常回喂 | ✅ | 失败回喂 `str(result)`（"错误: <error>"），模型可感知失败自愈（[react.py:308](../../../app/domain/reasoning/react.py#L308)）；`error`/`error_code` 进证据链记录（[react.py:310](../../../app/domain/reasoning/react.py#L310)） |
| 6 | 无效工具名处理 | ✅ | NOT_REGISTERED 失败走同一失败回喂分支，模型可见「工具未注册」；证据链记录 `error_code`（同 #5） |
| 7 | 解析 / JSON 失败降级 | ✅ | 解析失败不执行工具，构造失败 ToolResult（JSON_PARSE）走失败回喂——模型可见原因自纠，错误码进证据链（[react.py:290-303](../../../app/domain/reasoning/react.py#L290-L303)） |
| 8 | LLM 错误分类重试 | ✅ | 分工正确：LLM 层 RetryHandler（分类 + 指数退避 + fallback + 熔断），ReAct 层对 `StreamResult.error` 短路不空转（[react.py:145-161](../../../app/domain/reasoning/react.py#L145-L161)） |
| 9 | 工具结果回喂 | ✅ | tool_call_id 配对（防 400）+ 截断 2000 字符并带 `[结果已截断]` 标记（[react.py:348-358](../../../app/domain/reasoning/react.py#L348-L358)），模型可知结果不完整 |
| 10 | 上下文预算管理 | ✅ | context_manager 统一提供（经 ContextBudgetPort 注入 Agent）：轮次滑动窗口（保 assistant/tool 配对）+ token 预算硬上限（复用 TokenCounter），模型调用前作为 gatekeeper |
| 11 | 事件 / 回调体系 | ✅ | SSE 事件（reasoning/message/tool_call/tool_result/info/done）+ BaseAgent 钩子（on_thought/on_tool_call/on_tool_result/on_complete），与工业级 `on_tool_start/end` 同构 |
| 12 | 步数 / token 统计 | ✅ | outcome 含 iterations/total_tokens/usage/tool_calls（duration/success）+ ToolStats + Auditor |
| 13 | 流式输出 | ✅ | reasoning_content 与 content 分事件流式输出（[react.py:125-132](../../../app/domain/reasoning/react.py#L125-L132)） |

### 增强项对照

| # | 特性 | 状态 | 备注 |
| --- | --- | --- | --- |
| 14 | 并行工具执行 | ✅ | `asyncio.gather` 保序 + 信号量 + per-tool 锁（[react.py:299](../../../app/domain/reasoning/react.py#L299)），比 LangChain 默认串行更先进 |
| 15 | 结构化输出约束 | ✅ | Final Answer 工具模式：`output_schema` 注入 final_answer 工具，模型最后调用提交结构化结果并终止循环（模型原生、无额外调用）；参数校验失败回喂（VALIDATION）自纠 |
| 16 | guardrail | ❌ | 无输入 / 输出 guardrail（增强项，当前单用户本地场景不强制） |
| 17 | 权限 / 审批 | ✅ | ApprovalGate / RiskLevel L0-L3 / 审计——工具层工程护栏完整（`app/integration/tools/security.py`） |
| 18 | 自动摘要 / compaction | ❌ | 无（当前 max_iterations 不高、上下文不大，可延后） |
| 19 | 断点续跑 / 持久化 | ❌ | 无（任务层有会话管理，ReAct 循环无） |
| 20 | 成本上限 | ⚠️ | CostTracker 记账，无 cost ceiling 自动停机 |
| 21 | 沙箱 / 安全执行 | ⚠️ | 本项目工具为注册式（非任意代码执行），风险形态不同，无需 AST 沙箱；以风险分级 + 审批替代 |
| 22 | 最终答案校验 | ❌ | 无 `final_answer_checks`（证据链报告的答案校验是后续产物层的事） |
| 23 | 错误处理策略可扩展 | ⚠️ | ReAct 层错误处理硬编码，无 error_handlers 注册机制；工具层有 hooks 可扩展 |

---

## 发现的问题（按优先级）

> ✅ 无遗留问题——核心必备 13 项全部完成，增强项按「不做或预留」原则降级（见产品导向视角）。

---

## 产品导向视角

按第一硬性要求「服务主链路（拆分 → 并行排查 → 证据链报告）」检验：

- **P0（工具失败回喂）直接踩证据链**：良率工程师需要在根因报告里看到「哪个工具调用失败、为什么失败」；工具失败空串回喂使主 Agent 在分支排查中无法自愈——P0 优先级成立。
- **架构层（策略抽离、原语复用）间接推进主链路**：`execute_tool_calls` 供 PlannerAgent / ReflectionAgent 复用，为多 Agent 编排铺路。
- **增强项（guardrail / compaction / 断点续跑）不服务当前单用户本地场景，当前不做是对的**——符合项目「不做或预留」的降级原则。

---

## 修复优先级建议

> ✅ 核心必备 13 项全部完成，无待办项。
>
> ✅ 已完成（2026-08-27）：时间上限（`asyncio.timeout` 包裹循环 + 超时降级，生产值 `agent_timeout=300`）。
> ✅ 已完成（2026-08-27）：工具失败回喂 + 无效工具名处理（失败回喂 `str(result)`，error/error_code 进证据链）；AGENT-001 回归（except 显式元组）。
> ✅ 已完成（2026-08-27）：解析 / JSON 失败降级（解析失败不执行工具，构造失败 ToolResult 回喂 + JSON_PARSE 证据链）。
> ✅ 已完成（2026-08-27）：截断标记（工具结果截断带 `[结果已截断]`，模型可知结果不完整）。
> ✅ 已完成（2026-08-27）：reasoning_content 回喂策略（此前 P2 标注为错误外推——DeepSeek V4 thinking + tools 必须回喂；已加 `has_reasoning` 信号覆盖空 reasoning，防 400）。
> ✅ 已完成（2026-08-28）：上下文预算管理（context_manager 统一 + ContextBudgetPort 注入，轮次 + token 双层护栏）。

---

## 相关文档

- [ReActStrategy 策略组件](react.md)（本模块对外接口）
- [推理策略模块](reasoning.md)（主文档）
- [ReActAgent 桥接组件](../agent_doc/executor.md)
- [问题记录 AGENT-001](../../../issues/domain/agent/2026-08-17-except-comma-tuple-semantics.md)（except 逗号语法回归）
- [Agent 模块对外接口文档](../agent_doc/agent.md)
