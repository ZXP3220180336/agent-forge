# ReActStrategy 设计文档

> **模块**：`app/domain/reasoning/react.py`
> **更新日期**：2026-09-10
> **职责**：ReAct 原子推理策略——推理 ↔ 工具调用的完整循环算法（含工具并行原语、错误分发、结构化最终答案、上下文预算 + 成本上限 + 循环停滞护栏）
> **状态**：✅ 已实现
> **配套**：桥接见 [executor.md](../agent_doc/executor.md)（`ReActAgent`）；工业级对标见 [react_benchmark.md](react_benchmark.md)

---

## 📋 目录

- [ReActStrategy 设计文档](#reactstrategy-设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [ReAct 循环（推理 → 行动 → 观察）](#react-循环推理--行动--观察)
    - [finish\_reason 分支判定](#finish_reason-分支判定)
    - [错误处理分发（ErrorHandlerRegistry）](#错误处理分发errorhandlerregistry)
    - [成本上限（CostLimiterPort）](#成本上限costlimiterport)
    - [循环停滞检测（动作指纹 + STALLED）](#循环停滞检测动作指纹--stalled)
    - [模型拒答（REFUSED）](#模型拒答refused)
    - [多工具失败聚合与仲裁](#多工具失败聚合与仲裁)
    - [结构化最终答案（final\_answer 工具）](#结构化最终答案final_answer-工具)
    - [上下文预算（ContextBudgetPort）](#上下文预算contextbudgetport)
    - [工具结果回喂与截断](#工具结果回喂与截断)
    - [reasoning\_content 回喂](#reasoning_content-回喂)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [终止分支](#终止分支)
    - [可恢复分支](#可恢复分支)
    - [支撑方法（\_finalize\_outcome / \_finalize\_terminal / \_dispatch）](#支撑方法_finalize_outcome--_finalize_terminal--_dispatch)
    - [工具并行原语（execute\_tool\_calls）](#工具并行原语execute_tool_calls)
    - [ReActOutcome（结果载体）](#reactoutcome结果载体)
  - [执行流程](#执行流程)
  - [对外接口](#对外接口)
  - [边界情况](#边界情况)
  - [配置项清单](#配置项清单)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [问题记录](#问题记录)
  - [相关文档](#相关文档)

---

## 设计目标

1. **原子推理算法**：`execute()` 承载完整 ReAct 主循环，被 agent/ 层编排调用——编排职责（生命周期 / 状态 / 结果组装）在 agent/，算法在本模块
2. **可复用工具原语**：`execute_tool_calls()` 独立成原语（并行执行 + 保序），供 ReAct 循环自身执行工具、并经 `ReActAgent._execute_tool_calls` 转发保持既有测试兼容；Reflection / Planner 的收集 / 执行阶段复用完整 `ReActStrategy.execute`（而非裸原语，见 [reflection.md](reflection.md) / [planner.md](planner.md)）
3. **错误处理横切**：各终止 / 可恢复错误经 `ErrorHandlerRegistry` 按 kind 分发（CONTINUE / STOP / RAISE），默认行为 = 现有逻辑，调用方可注册覆盖
4. **结构化最终答案**：`output_schema` 启用时注入 final_answer 工具，模型最后调用提交 schema 约束结果并终止循环（兼作终止机制，无额外 LLM 调用）
5. **上下文预算护栏**：模型下次调用前经 `ContextBudgetPort` 裁剪（轮次 + token 双层），防上下文膨胀
6. **统一执行护栏**：每轮付费调用前和成功归账后通过 `_common.evaluate_guard` 检查取消、绝对 deadline 与累计成本；最终请求上下文超限进入同一类型化优先级
7. **纯算法依赖方向**：只依赖 ports + shared + 标准库，收标量参数（非 `AgentContext`）——可独立测试、可被任意编排复用

---

## 核心概念解释

### ReAct 循环（推理 → 行动 → 观察）

ReAct 是「推理 → 行动 → 观察」的循环：LLM 每轮推理产出 `finish_reason`，据此决定调用工具（行动）并回喂结果（观察）、正常结束或重试。`execute()` 承载主循环，各终止 / 错误分支拆分为职责单一的方法（`_finalize_*` / `_handle_*`），以 `outcome is not None` 作为终止信号。

### finish_reason 分支判定

LLM 单轮回复的 `finish_reason` 决定下一步：

| finish_reason | 语义 | 处理 |
| --- | --- | --- |
| `tool_calls` | 模型请求调用工具 | final_answer 检测 → 执行工具 → 结果回喂 → 下一轮 |
| `tool_calls` + 空 `tool_calls` / 无工具可用 | 协议信号不一致（声明调工具却没给出 / 系统未注册工具） | `_handle_tool_protocol_error` 协议异常 → `PARSE_FAILED` 分发（默认重试） |
| `stop` / `length` | 正常生成完毕（length 为截断） | `_finalize_outcome` 组装正常终态 |
| 空 + 无内容 | 模型未生成有效输出 | `_handle_empty_output` 错误分发（默认重试） |

### 错误处理分发（ErrorHandlerRegistry）

错误处理是共享内核横切能力（`app.shared.error_handling`）：按 `AgentErrorKind` 注册 handler，决策 `CONTINUE`（回喂继续）/ `STOP`（终止）/ `RAISE`（上抛 `AgentRunError`）。ReAct 循环内触发 13 类 kind（`UNKNOWN` 主循环兜底 / `CANCELLED` 优雅取消，BaseAgent 仍保留 `asyncio.CancelledError` 硬取消与逃逸异常兜底）：

| `AgentErrorKind` | 触发场景 | 默认 action |
| --- | --- | --- |
| `LLM_FAILED` | LLM 调用失败（`StreamResult.error`） | STOP（短路失败；handler 可 CONTINUE 重试，重试受 `max_llm_fail_retries` 上限硬终止） |
| `EMPTY_OUTPUT` | 空输出 | CONTINUE（重试，连续超过 `max_empty_retries` 硬终止） |
| `MAX_TURNS` | 迭代耗尽 | STOP（兜底） |
| `TIMEOUT` | 总时长超限 | STOP（降级） |
| `COST_EXCEEDED` | 累计成本超限 | STOP（停机降级） |
| `STALLED` | 连续相同工具调用（工具+参数） | STOP（停机） |
| `REFUSED` | 模型拒答（refusal 字段 / content_filter） | STOP（停机） |
| `UNKNOWN` | 未捕获异常（主循环 except 兜底） | STOP（保留部分进度） |
| `CANCELLED` | 用户取消（cancel_event 置位） | STOP（优雅停止，保留部分进度） |
| `CONTEXT_EXCEEDED` | 最终请求超过模型上下文窗口 | STOP（保留部分进度） |
| `TOOL_FAILED` | 工具执行失败 | CONTINUE（回喂） |
| `PARSE_FAILED` | 工具参数 JSON 解析失败 / finish_reason=tool_calls 但无 tool_calls（协议异常） | CONTINUE（回喂/重试） |
| `STRUCTURED_INVALID` | final_answer 参数校验失败 | CONTINUE（回喂） |

**handler 异常防御**：handler 是调用方扩展点，其自身异常不破坏主循环——`dispatch` 捕获 `Exception`（不含 `BaseException`，`CancelledError` 穿透）后记日志并按该 kind 默认 action 降级（= 未注册行为），扩展点缺陷可观测且不掩盖被分发的原始错误（见 [SHARED-001](../../../issues/shared/error_handling/2026-08-30-handler-exception-defense.md)）。

**error 文本取舍（LLM_FAILED 不脱敏 vs UNKNOWN 脱敏）**：`LLM_FAILED` 的 error 保留整流层失败原因（如 401/429/超时），对良率工程师诊断有直接价值，**不脱敏**（整流层仅截断 500 字符）；`UNKNOWN` 只保留异常类型名（**脱敏**，完整异常进日志）——「未知异常」无诊断价值且异常文本可能含内部路径/敏感值，两类取舍不同（见 [REASON-005](../../../issues/domain/reasoning/2026-08-30-unknown-error-redaction.md)）。

### 成本上限（CostLimiterPort）

成本上限是横切护栏，由应用层 `CostLimiter` 结构实现（经 `CostLimiterPort` 注入；成本估算经 `LLMGateway.calculate_cost` 取）。ReAct 在每轮 LLM 调用前检查调用方累计基线，避免基线已经超限时仍多发一笔请求；完整响应返回后先归并本轮 usage，再复查并在工具副作用或重试前停机。`cost_limiter=None` 时不做成本估算。**判定口径 = 调用方累计基线 + 本轮局部**：`baseline_usage` 只参与成本判定，不进入本次 `outcome.usage` / `total_tokens`，由调用方各归并一次防双计。

取消、deadline、成本和最终请求上下文超限统一使用 `_common.GuardResult`，固定优先级为 `CANCELLED > TIMEOUT > COST_EXCEEDED > CONTEXT_EXCEEDED`。成功调用先接管 content/reasoning/usage，再应用该优先级；因此同时发生取消与成本超限时按取消收尾，但真实 usage 仍保留。

### 循环停滞检测（动作指纹 + STALLED）

死循环护栏：模型「反复调用同一工具同一参数」原地打转时，靠 `max_same_action_turns`（默认 3）主动停机——连续相同工具调用（工具+参数）超过上限走 `STALLED` 分发硬终止（默认 STOP），**不执行本轮工具**（防重复副作用 / 烧钱）。动作指纹 = 本轮 tool_calls 的（名, 规范化参数）序列化：参数 `json.loads` 后 `sort_keys=True` 重 dump（key 顺序 / 空白不同指纹一致），非法 JSON 回退原始串；换工具 / 换参数重置计数；`final_answer` 不参与（终止工具）；STALLED handler 可 RAISE 上抛（对齐 COST_EXCEEDED 终结护栏）。

### 模型拒答（REFUSED）

模型拒答（内容安全策略触发）基于**显式信号**判定（LLM-004 原则）：`refusal` 字段非空或 `finish_reason=content_filter` → 主循环走 `REFUSED` 分发硬终止（默认 STOP），**不误判为成功答案、不空转重试**。error 记录「模型拒答: <截断文本>」（拒答文本截断，LLM-008 基线——拒答常引用触发内容，完整文本不落盘）。DeepSeek 无 refusal 字段的 stop+空 content 属「空回答」而非显式拒答，保持正常空回答语义（不靠 content 空推断拒答）。

### 多工具失败聚合与仲裁

同轮多个工具失败的处理意图可能不同（上报 / 终止 / 回喂）——先按 kind 分组聚合（同 kind 失败原因合并为一条 message 给 handler），再逐 kind 分发。`RAISE` 由 `_dispatch` 立即抛出；没有 RAISE 时，已取得的决策按 `STOP > CONTINUE` 仲裁。终止 / 上报时其他失败不回喂（循环结束，回喂无意义），但全部失败已进证据链记录。

### 结构化最终答案（final_answer 工具）

`output_schema` 启用时，注入 `final_answer` 工具（参数 = schema）。模型完成任务后调用一次提交结构化结果，`_handle_final_answer` 解析 + jsonschema 校验：成功即终止循环并写入 `outcome.structured`；校验失败走 `STRUCTURED_INVALID` 分发（默认回喂错误文本，模型下轮自纠）。注入工具非注册工具，识别在主循环，不经过 `execute_tool_calls`。

### 上下文预算（ContextBudgetPort）

上下文预算是横切护栏，由应用层 `context_manager` 统一实现（经 `ContextBudgetPort` 注入），ReAct 不实现算法。置于**循环顶部、每次 LLM 调用前裁剪**（所有继续路径共用：工具回喂 / LLM 失败重试 / final_answer 回喂重试 / 空输出重试——否则非工具路径上下文无限增长、预算失效）：保留 system/user 前缀 + 最近 N 轮 assistant/tool 配对（`max_context_rounds`）+ token 硬上限（`max_context_tokens`）。选 trimming 而非摘要——摘要压缩工具原始记录会破坏证据链（产品核心）。

### 工具结果回喂与截断

工具结果回喂模型时：成功回喂 `content`，失败回喂 `str(result)`（`"错误: <error>"`，见 `ToolResult.__str__`）——模型需看到失败原因才能自愈。回喂文本截断：tool 消息 2000 字符、tool_result 事件 200 字符，截断处追加 `[结果已截断]` 标记（预留标记长度，总长不超限）——模型可知结果不完整，可缩小范围重查。

### reasoning_content 回喂

DeepSeek V4 thinking 模式带 tools 时必须回喂 `reasoning_content`（否则 400）。`StreamResult.has_reasoning` 区分「未返回」与「返回空」：未返回不回喂（chat 模型），返回空也回喂空串（thinking 模型）——字段始终存在。

---

## 架构总览

```text
ReActAgent._strategy_cycle()（agent/ 层编排：生命周期 / 状态 / 结果组装）
        ▼ execute()
ReActStrategy.execute()（ReAct 主循环）
    ├── ContextBudgetPort.trim_messages ──► 上下文预算（模型调用前裁剪）
    ├── CostLimiterPort.check ────────────► 成本上限（usage 累加后折算成本，超限停机）
    ├── LLMGateway.async_generate ────────► LLM 推理（流式 reasoning/message 事件，stream_mode=True 默认）
    │    └── LLMGateway.generate ─────────► 非流式通道（stream_mode=False：一次拿 StreamResult + 合成整条事件）
    ├── ErrorHandlerRegistry（shared）─────► 12 类 AgentErrorKind 分发（CONTINUE/STOP/RAISE）
    ├── execute_tool_calls() ─────────────► 工具并行执行原语
    │    └── ToolGateway.execute ─────────► 单工具执行（gather 保序）
    └── ReActOutcome ─────────────────────► 结果载体（execute() 后读取）
```

**分层职责**：

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 编排层 | `ReActAgent`（agent/executor.py） | 生命周期 / 状态 / 事件路由 / `ReActOutcome → AgentResult` 组装 |
| 策略层 | `ReActStrategy` | ReAct 主循环算法 + 错误分发 + 工具并行原语 |
| 端口层 | `LLMGateway` / `ToolGateway` / `ContextBudgetPort` / `CostLimiterPort` | 外部依赖抽象（依赖倒置，见 [ports.md](../ports_doc/ports.md)） |
| 共享内核 | `ErrorHandlerRegistry` / 事件构造 | 错误分发横切 + SSE 事件（`app.shared.*`） |

**依赖方向**：本模块只依赖 ports + shared + 标准库，**不 import agent/**。

---

## 组件详解

### 终止分支

| 方法 | 触发 | 默认行为 |
| --- | --- | --- |
| `_finalize_outcome` | finish_reason=stop/length/有内容，或其他分支完成终态组装 | 写入 `ReActOutcome` 并生成可选 info 与唯一 done；正常结束时 `success = content 非空` |
| `_handle_llm_failed` | `StreamResult.error` 非空 | STOP 短路失败（`success=False` + error）；handler 可 CONTINUE 重试 / RAISE 上抛；重试受 `max_llm_fail_retries` 上限硬终止（对齐空输出护栏） |
| `_finalize_max_turns` | 循环达到 `max_iterations` | STOP 兜底：用 `last_visible_result` 组装 outcome，error 记录「已达到最大迭代次数(N)」 |
| `_finalize_guard_result` | 共享护栏返回 CANCELLED / TIMEOUT / COST_EXCEEDED / CONTEXT_EXCEEDED | 统一映射各类终态文案并调用 `_finalize_terminal`；保留当前轮或上一轮可见成果与可得 usage，handler 可按错误类型决定 STOP / RAISE |
| `_finalize_stalled` | 连续相同工具调用超过 `max_same_action_turns` | STOP 停机：组装 outcome（content 空，保留 reasoning），error 记录「连续 N 轮相同工具调用」；本轮工具不执行 |
| `_finalize_refused` | refusal 字段 / content_filter（显式拒答） | STOP 停机：content 保留，error 记录「模型拒答: <截断文本>」；不误判为成功答案、不空转重试 |
| `_finalize_unknown` | 主循环未捕获异常；包括 timeout scope 未到期时由 LLM/结算/日志抛出的普通 `TimeoutError` | STOP 兜底：当前轮有可见进度时优先使用，否则回退 `last_visible_result`；合并未归账 usage，error 记录「Agent 运行异常: <异常类型名>」（脱敏，完整异常进日志） |

关键语义：`asyncio.timeout_at` 包业务循环，在 `max_execution_time` 到期时触发当前 task 取消（非单轮预算）。传给 LLM 的内部 deadline 提前 `min(1s, 总时长 × 10%)`，为流关闭、预留结算和日志收尾留出有界窗口。ReAct 保存 timeout scope，仅在 `expired()` 为真时把内置 `TimeoutError` 解释为总执行超时；内部组件抛出的同名异常归 UNKNOWN。领域终态分发和 done 生成在 scope 外完成且不发起新 LLM/工具副作用；同步阻塞或吞取消代码可能形成额外尾部。终止降级还会判别「真异常 vs 生成器被 finalizer 关闭」（慢消费者场景 aclose 由不同 task 驱动），后者干净停止、不 yield 降级事件。

### 可恢复分支

| 方法 | 触发 | 默认行为 |
| --- | --- | --- |
| `_handle_tool_protocol_error` | finish_reason=tool_calls 但 tool_calls 为空（协议信号不一致） | `PARSE_FAILED` 分发：默认 CONTINUE 重试（不入空输出计数 / 不进停滞检测 / 不执行空工具列表）；handler 可 STOP 终止 / RAISE 上抛 |
| `_handle_empty_output` | finish_reason 空 + content 空 | CONTINUE 重试；连续超过 `max_empty_retries` 硬终止（handler 可 STOP / RAISE，CONTINUE 被忽略） |
| `_handle_tool_calls` | finish_reason=tool_calls | 调 `execute_tool_calls` 执行 → 失败工具按 kind 聚合分发 + 仲裁 → 全 CONTINUE → 继续循环（预算在循环顶部统一裁剪，见上下文预算节） |
| `_handle_final_answer` | 检测到 final_answer 工具调用 | 成功提取 → 终止写 `outcome.structured`；校验失败 → `STRUCTURED_INVALID` 分发（默认回喂自纠） |

### 支撑方法（_finalize_outcome / _finalize_terminal / _dispatch）

- `_finalize_outcome(*, success, content, reasoning, iteration, total_usage, error, info_message, structured) -> list[str]`：统一收尾——组装 outcome + 返回收尾事件列表（可选 info + done 恰一次），供各终结 / STOP 分支复用（dispatch 由调用方负责——CONTINUE 语义各异：重试 / 回喂 / 忽略）；普通 def（无 await）
- `_finalize_terminal(kind, message, iteration, *, success, content, reasoning, total_usage, error, info_message, structured) -> list[str]`：终结性错误统一收尾——dispatch（RAISE 上抛，CONTINUE 忽略）→ 复用 `_finalize_outcome`；供执行护栏、拒答、UNKNOWN、达到最大轮次及硬重试上限等无恢复语义的分支复用
- `_dispatch(kind, message, iteration) -> AgentErrorAction`：错误分发唯一入口——委托共享 `_common.dispatch_error`（`RAISE` 决策抛 `AgentRunError`，否则返回 action；react 循环内 8 处 `_dispatch` 调用统一收敛）

契约约定：收尾/处理分支方法（`_finalize_*` / `_handle_final_answer` / `_handle_empty_output`）为普通或 async 方法，**返回 `list[str]` 收尾事件**（info/done/error，一次性）；主循环 `for e in await X(...): yield e` 转发。仅以下保持 async-generator（逐 token / 逐条实时事件流）：`execute()`（公共入口）、`execute_tool_calls()`（逐工具事件）、`_handle_tool_calls()` / `_llm_round_non_streaming()`（内部转发流式调用）。

### 工具并行原语（execute_tool_calls）

```python
async def execute_tool_calls(self, tool_calls: list[dict], messages: list[dict], iteration: int,
                             tool_timeout: int | None = None, tool_max_retries: int | None = None) -> AsyncGenerator[str]:
```

`asyncio.gather` 并行执行所有工具（并发度由 ToolService 信号量 `agent_max_concurrent_tools` 限制），gather 保证结果顺序 = 输入顺序——OpenAI 兼容 API 要求 tool 消息与前置 assistant.tool_calls 的 `tool_call_id` 配对，顺序不能乱。并发 task 内只做执行不 yield 事件（避免事件交错）；SSE 事件只在主 generator 内按序 yield。工具参数 JSON 解析失败不静默用空参执行（会掩盖错误 / 可能触发副作用），构造失败 `ToolResult`（JSON_PARSE）走失败回喂。

### ReActOutcome（结果载体）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `content` | `str` | 最终回答 |
| `reasoning` | `str` | 末轮推理内容（`reasoning_content`；跨轮完整推理链见 SSE 事件流，outcome 不跨轮累计） |
| `structured` | `dict \| None` | 结构化最终答案（final_answer 产出，output_schema 启用时） |
| `tool_calls` | `list[dict]` | 工具调用记录（tool/params/result/success/error/error_code/duration） |
| `iterations` | `int` | 实际轮数 |
| `total_tokens` / `usage` | `int` / `dict \| None` | Token 统计（累计） |
| `error` | `str \| None` | 失败原因（LLM 调用失败 / 无结果 / 超时 / 按策略终止） |
| `success` | `bool` | 是否成功 |

桥接方（`ReActAgent._map_outcome`）将 `ReActOutcome` 组装为 `AgentResult`。

---

## 执行流程

```text
第 N 轮推理开始（for iteration in 1..max_iterations）
  ├─ 1. 统一调用前护栏（cancel > deadline > cost）→ 类型化终态，不开始新轮
  ├─ 2. 上下文预算：ContextBudgetPort.trim_messages（每次 LLM 调用前裁剪，所有继续路径共用）
  ├─ 3. LLM 推理（stream_mode 双通道）：
  │     ├─ True（默认）→ async_generate 流式（cancel_event 传给 LLM 层中断调用），yield reasoning/message 逐 token，累计 usage
  │     └─ False → generate() 非流式一次拿 StreamResult（model_key="main"），透传 cancel/deadline 并合成整条事件；
  │           失败契约：None（可恢复耗尽）/ AppError（共享树：LLMAPIError + 熔断）→ LLM_FAILED；
  │           AppError 树外异常 → 外层 UNKNOWN；reserve/create 内受控，结算后再做终止复查
  ├─ 4. 先归并本轮成果与 usage，再统一复查 cancel > deadline > cost
  │       （命中即按类型化终态收尾，不允许失败重试 / 工具执行再产生副作用；
  │         基线 = execute 的 baseline_usage，跨阶段复用方注入，报告口径仍局部）
  ├─ 5. stream_result.error 非空？
  │       └─ LLM 失败 → _handle_llm_failed（STOP 短路 / CONTINUE 重试，重试受 max_llm_fail_retries 上限硬终止）
  ├─ 6. 模型拒答（refusal 字段 / content_filter）→ _finalize_refused（默认 STOP 停机，
  │       不误判为成功答案、不空转重试；DeepSeek stop+空 content 保持空回答语义）
  ├─ 7. 追加 assistant 消息（reasoning_content 按 has_reasoning 回喂 + tool_calls 配对，防 400）
  ├─ 8. 空输出连续计数：本轮有产出（工具调用/stop/length/有内容）→ 清零；空输出 → +1
  ├─ 9. finish_reason 分支：
  │     ├─ "tool_calls" 但无 tool_calls → _handle_tool_protocol_error（协议异常 → PARSE_FAILED 分发，
  │     │       默认重试，不入空输出计数 / 不进停滞检测 / 不执行空工具列表）
  │     ├─ "tool_calls" 且有工具 →
  │     │     ├─ 含 final_answer？→ _handle_final_answer（成功终止 / 校验失败回喂）
  │     │     ├─ 停滞检测：连续相同工具调用超 max_same_action_turns → _finalize_stalled（不执行工具）
  │     │     └─ _handle_tool_calls：execute_tool_calls 并行执行 → 失败工具按 kind 聚合分发 + 仲裁
  │     │           → 全 CONTINUE → 下一轮
  │     ├─ "stop"/"length"/有内容 → _finalize_outcome（正常结束）
  │     └─ 空输出 → _handle_empty_output（默认 CONTINUE 重试；连续超 max_empty_retries 硬终止；STOP → 终止）
  ├─ 10. 循环耗尽 → _finalize_max_turns（默认 STOP：last_visible_result 兜底）
  ├─ 11. 内部 deadline / timeout scope.expired() → _finalize_guard_result(TIMEOUT)（保留当前轮部分成果）
  └─ 12. 内部普通 TimeoutError / 其他未捕获异常 → _finalize_unknown（默认 STOP：保留当前轮部分进度）；
        AgentRunError（RAISE 决策）前置 re-raise 不被吞；asyncio.CancelledError / GeneratorExit
        是 BaseException 不被捕获（保持 CANCELLED / 生成器关闭语义）
```

---

## 对外接口

> 仅列模块对外暴露的公共接口（被 agent/ 层编排或调用方依赖）；`_` 前缀私有方法（终止 / 可恢复分支）见组件详解。

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `__init__(llm, tools, context_budget=None, error_handlers=None, cost_limiter=None)` | 构造 | 注入端口依赖（LLMGateway / ToolGateway）+ 横切能力（ContextBudgetPort / ErrorHandlerRegistry / CostLimiterPort） |
| `execute(user_input, messages, *, max_iterations, temperature, max_tokens, max_execution_time=None, max_context_rounds=None, max_context_tokens=None, max_empty_retries=2, max_llm_fail_retries=2, max_same_action_turns=3, tool_timeout=None, tool_max_retries=None, output_schema=None, stream_mode=True, cancel_event=None, baseline_usage=None) -> AsyncGenerator[str]` | 异步生成器 | ReAct 主循环；yield SSE 事件（reasoning/message/tool_call/tool_result/info/done），结果写入 `outcome`。`stream_mode`：True=流式 async_generate（默认，逐 token）；False=非流式 generate()（整条 reasoning/message 事件，后台子 Agent 无人订阅场景，Phase C）。`baseline_usage`：跨阶段复用方（planner 步骤子跑）注入调用方累计用量，仅参与成本判定、不进报告口径 |
| `execute_tool_calls(tool_calls, messages, iteration, tool_timeout=None, tool_max_retries=None) -> AsyncGenerator[str]` | 异步生成器 | 工具并行执行原语（gather 保序 + 事件产出 + 记录；`tool_timeout`/`tool_max_retries` 透传 ToolGateway，None=走执行器全局）；现独立入口：`ReActAgent._execute_tool_calls` 转发（既有测试兼容）。Reflection / Planner 的收集 / 执行阶段复用完整 `execute`（[reflection.md](reflection.md) / [planner.md](planner.md)） |
| `outcome` | 实例属性 | `ReActOutcome \| None`，`execute()` 结束后读取 |

**最小调用示例**：

```python
from app.domain.reasoning import ReActStrategy

strategy = ReActStrategy(llm=llm_service, tools=tool_service)
messages = [{"role": "user", "content": "30C 转华氏"}]
async for event in strategy.execute(
    "30C 转华氏", messages,
    max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=30.0,
):
    yield event  # 转发 SSE 事件
result = strategy.outcome  # ReActOutcome
```

---

## 边界情况

1. **LLM 调用失败**（`StreamResult.error` 非空）→ `_handle_llm_failed`，默认 STOP 短路 `success=False` + error；不把「失败」当「空输出」空转重试（浪费 LLM 调用 + 错误信息不准确）。**重试上限**（`max_llm_fail_retries`，默认 2）→ 连续失败计数（失败轮 +1、成功轮清零、取消不参与），超过上限在 CONTINUE 分支硬终止（error「连续 LLM 调用失败（N 轮）」；即使 handler CONTINUE 也终止，防空转烧钱）；`0` = 首次失败即终止
2. **空输出**（finish_reason 空 + content 空）→ `_handle_empty_output`，默认 CONTINUE 重试下一轮
3. **达到 `max_iterations`** → `_finalize_max_turns`，用 `last_visible_result` 兜底强制结束，error 记录「已达到最大迭代次数(N)」（无最近可见结果时 `success=False`）
4. **达到 `max_execution_time`**（None=不设限）→ `_finalize_guard_result(TIMEOUT)`：当前轮有可见进度时优先保留当前轮，否则用 `last_visible_result` 降级；异常携带 usage 优先且只归账一次。`last_visible_result` 仅由非空 content/reasoning 更新，未执行 tool_calls 不会覆盖已有成果。LLM 内部 deadline 提前预留有界清理窗口；只有 timeout scope `expired()` 才认定总执行超时，内部普通 `TimeoutError` 归 UNKNOWN；慢消费者关闭生成器时干净停止。`max_execution_time` 是业务循环的 task 取消触发点，领域终态分发和 done 生成位于 scope 外且不再发起 LLM/工具副作用；同步阻塞或吞取消扩展点不受绝对返回时限保证。
5. **累计成本超限**（`agent_max_cost`，None=不启用）→ 每轮调用前检查基线 + 局部累计，调用后先归账再复查；超限走 `COST_EXCEEDED` 分发。`baseline_usage` 只参与判定、不进报告口径；与取消或 deadline 同时命中时遵守共享优先级
6. **工具参数 JSON 解析失败** → 不执行工具：构造失败 ToolResult（JSON_PARSE）回喂模型自纠，`error`/`error_code` 进证据链
7. **工具执行失败 / 无效工具名** → 回喂 `str(result)`（`"错误: <error>"`，无效工具含「未注册」），模型可感知失败自愈；`error` / `error_code` 进证据链
8. **工具结果超长** → 截断（tool 消息 2000 字符 / 事件 200 字符）并追加 `[结果已截断]` 标记（预留标记长度，总长不超限）
9. **reasoning_content 回喂** → DeepSeek V4 thinking + tools 必须回喂（否则 400）；`has_reasoning` 覆盖空 reasoning（空串也回喂），无信号不回喂（chat 模型）
10. **上下文预算**（`max_context_rounds` / `max_context_tokens`，None=不裁剪）→ 循环顶部、每次 LLM 调用前经注入的 ContextBudgetPort 裁剪（所有继续路径共用）：保留最近 N 轮 assistant/tool 配对 + token 硬上限
11. **结构化最终答案**（`output_schema`，None=不启用）→ 注入 final_answer 工具；模型调用即终止产出 `outcome.structured`；参数校验失败回喂（VALIDATION/STRUCTURED_INVALID）自纠
12. **错误处理分发**（`error_handlers`，None=默认行为）→ 各终结/可恢复错误按 kind 分发（CONTINUE/STOP/RAISE）；默认 = 现有行为，调用方按 kind 注册覆盖
13. **多工具失败** → 按 kind 聚合（同 kind 原因合并给 handler）；RAISE 在 `_dispatch` 中立即传播，否则 STOP 优先于 CONTINUE；终止/上报时其他失败不回喂，但全部失败已进证据链
14. **空输出重试上限**（`max_empty_retries`，默认 2）→ 连续空输出计数，超过上限在空输出分支硬终止（`error` 记录「连续空输出（N 轮）」，先 dispatch 供 handler RAISE，CONTINUE 忽略）；有产出轮计数清零（非连续不累计）；LLM 失败重试轮不参与。**空输出重试轮不追加空 assistant 消息**（无产出不写历史，防累积污染上下文；thinking 空 reasoning 轮 `has_reasoning=True` 仍追加保字段）
15. **循环停滞检测**（`max_same_action_turns`，默认 3）→ 连续相同工具调用（工具+参数）超过上限 → STALLED 分发硬终止（本轮工具不执行，error 记录「连续 N 轮相同工具调用」）；参数规范化（key 顺序 / 空白不同指纹一致）；换工具 / 换参数重置；`final_answer` 不参与；STALLED handler 可 RAISE 上抛
16. **模型拒答**（refusal 字段 / content_filter）→ REFUSED 分发硬终止（默认 STOP，error 记录「模型拒答: <截断文本>」）；显式信号原则（LLM-004，不靠 content 空推断）——DeepSeek 无 refusal 字段的 stop+空 content 保持空回答语义；拒答文本截断（LLM-008 基线）
17. **未捕获异常**（UNKNOWN）→ 主循环 `except Exception` 兜底：当前轮有可见进度时优先用 `current_result`，否则用 `last_visible_result` 组装 outcome 并合并未归账 usage，保留部分进度 + 证据链；error 仅记录「Agent 运行异常: <异常类型名>」（**脱敏**——不拼接异常 message，完整异常含 traceback 进日志供运维诊断，产品侧不泄漏内部细节，见 [REASON-005](../../../issues/domain/reasoning/2026-08-30-unknown-error-redaction.md)）；RAISE 决策（`AgentRunError`）前置 re-raise 不被吞；`asyncio.CancelledError` / `GeneratorExit` 是 `BaseException`，保持 CANCELLED / 生成器关闭语义
18. **用户取消**（`cancel_event`，None=不启用）→ 调用前、成功归账后与类型化异常出口经同一 guard 判定，CANCELLED 优先于 deadline/cost/context；不重试并保留当前可见成果与真实 usage；`asyncio.CancelledError`（硬取消）仍走 BaseAgent.run 的独立路径
19. **协议异常**（`finish_reason=tool_calls` 但 `tool_calls` 为空 **或** 无工具可用）→ `_handle_tool_protocol_error` 短路为 `PARSE_FAILED` 分发：默认 CONTINUE 重试（不入空输出计数 / 不进停滞检测 / 不执行空工具列表，避免空转浪费轮次）；handler 可 STOP 终止（error 记录「协议异常」）/ RAISE 上抛。覆盖两类信号不一致：① 声明调工具却没给出 `tool_calls`；② 要调工具但系统未注册任何工具（`has_tools=False`）。
20. **非流式通道**（`stream_mode=False`，默认 True）→ 每轮 LLM 改走 `generate(model_key="main")` 一次拿完整 StreamResult，主循环护栏语义（成本/失败/拒答/工具/停滞/空输出）与流式一致；差异：① reasoning/message 事件为**整条一次性**（SSE 协议同构，前端打字机退化为整段）；② 失败契约对齐流式整流——`generate` 返回 None（可恢复耗尽）与抛 `AppError` 均折算 `LLM_FAILED` 分发（流式路径此情形从不走 UNKNOWN）；**共享 `AppError` 树含熔断 `CircuitBreakerOpenError`**（NonRetryableError 子类）→ 熔断同样归 LLM_FAILED（两通道一致）；仅 `AppError` 树外的编程错误冒泡外层 `UNKNOWN`；③ `cancel_event` 与绝对 deadline 进入 generate 内部，约束 reserve/create/retry，成功结算后仍复查一次；④ 无对应 settings 项（YAGNI，Phase C 子 Agent 构造 ctx 置 False；勿与仅作元数据出口的 `agent_streaming` 混淆）

---

## 配置项清单

配置经装配根注入 `AgentContext` → `ReActStrategy.execute()`（生产值覆盖，字段默认 None 向后兼容）：

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `agent_max_iterations` | int | 10 | `max_iterations` 生产值（迭代上限） |
| `agent_timeout` | int | 300 | `max_execution_time` 生产值（业务循环 timeout 取消触发点，秒） |
| `agent_max_context_rounds` | int | 8 | `max_context_rounds` 生产值（上下文预算保留轮数） |
| `agent_max_cost` | float \| None | None | 成本上限（美元 USD）；None=不启用（装配根据此构造 CostLimiter 注入，0 则任何正成本即停） |
| `agent_max_empty_retries` | int | 2 | 连续空输出重试上限：空输出最多重试 N 次，第 N+1 次仍空输出则终止（0=首次空输出即终止） |
| `agent_max_llm_fail_retries` | int | 2 | LLM 失败重试上限：LLM 调用失败最多重试 N 次，第 N+1 次仍失败则终止（0=首次失败即终止；对齐空输出护栏，防 handler CONTINUE 无限重试） |
| `agent_max_same_action_turns` | int | 3 | 循环停滞检测：连续相同工具调用（工具+参数）超过 N 轮，下一轮仍相同则 STALLED 终止 |

`AgentContext.max_context_tokens` 无独立 `agent_max_context_tokens` 配置——生产值复用全局 `max_context_tokens`（默认 128000，LLM 上下文窗口，见 [config](../../config_doc/config.md)）由装配根注入；轮次预算由 `agent_max_context_rounds`（8）配置。完整配置表见 [config 文档](../../config_doc/config.md)。

---

## 测试状态

`tests/unit/test_react_strategy.py` 覆盖分类：

- **工具循环**：stop 结束（outcome 组装）/ 空输出重试后结束 / 持续空输出 → 迭代兜底
- **工具原语**：并行保序（延迟交错，结果顺序 = 输入顺序）/ 实际并发（总耗时 < 串行和）/ timeout/max_retries 透传 ToolGateway（默认 None 走执行器全局）
- **时间上限**：首轮超时降级 / 中途超时保留部分进度 / 宽松上限不影响完成 / `None` 显式不设限 / 内部普通 `TimeoutError` 归 UNKNOWN / 外部 task cancel 与异 task `aclose()` 不被吞
- **统一护栏**：`cancel > deadline > cost > context` 直接契约 / 基线已超成本时零 LLM 调用 / 流式与非流式调用后取消优先于成本且 usage 不丢 / 宽松上限与未配置 limiter 不触发
- **空输出重试上限**：持续空输出达上限终止 / 恰好达上限仍重试 / 上限可配置 / 有产出后计数重置 / 达上限 handler RAISE 上抛 / 重试轮不追加空 assistant 消息
- **循环停滞检测**：同工具同参数达上限终止 / 未达上限正常 / 参数变化重置 / 换工具重置 / 上限可配置 / final_answer 不参与 / STALLED handler RAISE / 参数 key 顺序规范化指纹相同
- **模型拒答**：refusal 非空终止（content 保留）/ content_filter 终止 / REFUSED handler RAISE / CONTINUE 忽略
- **未捕获异常**：中途异常保留证据链 / UNKNOWN handler RAISE 抛 AgentRunError / CONTINUE 忽略 / error 脱敏（只留异常类型名，敏感 message 不泄漏，完整异常进日志）
- **优雅取消**：cancel_event 置位终止 / LLM error+置位 → CANCELLED 不重试 / 未置位正常
- **工具失败**：失败回喂 / 证据链记录 error+error_code / 无效工具名 NOT_REGISTERED / 解析失败不执行工具 + JSON_PARSE / 截断标记（带标记不超限 / 短结果无标记）
- **reasoning 回喂**：`has_reasoning` 回喂空串 / 无信号不回喂
- **上下文预算**：注入 ContextBudgetPort 后轮次裁剪生效 / 非工具路径（空输出重试）每次 LLM 调用前也裁剪
- **final_answer**：成功提取终止 / 校验失败回喂 / 未配置不注入
- **错误处理**：LLM_FAILED→CONTINUE 重试 / LLM_FAILED→RAISE 上抛 / EMPTY_OUTPUT→STOP / TOOL_FAILED→STOP（部分进度保留）/ STRUCTURED_INVALID→STOP / **LLM 失败重试上限**（持续失败硬终止 / 成功轮清零 / 达上限 RAISE / 0=首次即终止 / 默认 STOP 零变化）
- **多工具失败**：同 kind 聚合 message / 跨 kind STOP 仲裁 / 跨 kind RAISE 仲裁
- **协议异常**：finish_reason=tool_calls 空列表默认重试后正常结束 / 连续协议异常不入空输出计数（max_iterations 兜底）/ PARSE_FAILED handler STOP 终止 / RAISE 上抛 / 无工具场景同样识别 / **无工具 + 非空 tool_calls 短路**
- **handler 异常防御**：LLM_FAILED handler 抛异常默认 STOP（error 为 LLM 失败原因）/ TOOL_FAILED handler 抛异常默认 CONTINUE（回喂继续）/ UNKNOWN handler 抛异常兜底不崩（registry 层另经 test_error_handling 覆盖）

- **非流式通道**（`tests/unit/test_react_strategy_nonstream.py`）：单轮 stop、工具循环、失败分类、调用后 cancel/cost 优先级、usage 累计、空输出恢复、reasoning-only、final_answer 与双通道 outcome 对齐

另经 `tests/unit/test_agent.py`（10 用例）间接覆盖（`ReActAgent` 编排路径 + cost_limiter / max_empty_retries / max_same_action_turns / stream_mode 透传，见 [executor.md](../agent_doc/executor.md)）。

---

## 设计决策

| ADR | 一句话结论 |
| --- | --- |
| [react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md) | ReAct 算法从 agent/ 抽离至 reasoning/（原子策略），`execute_tool_calls` 原语化 |
| [agent-error-handling](../../../adr/domain/agent/2026-08-28-agent-error-handling.md) | 错误处理横切 ErrorHandlerRegistry，按 kind 分发（可恢复默认回喂、终结性默认终止） |
| [structured-output](../../../adr/domain/reasoning/2026-08-28-structured-output.md) | 结构化用 Final Answer 工具（模型原生，兼作终止）而非事后提取；`generate_structured` 留非 Agent 场景 |
| [context-budget](../../../adr/domain/reasoning/2026-08-28-context-budget.md) | 上下文预算归 context_manager（横切），经 ContextBudgetPort 注入；选 trimming 而非摘要（保证据链） |
| [cost-limit](../../../adr/domain/reasoning/2026-08-30-cost-limit.md) | 成本上限经 CostLimiterPort 注入（应用层经 LLMGateway.calculate_cost 取成本估算），超限走 COST_EXCEEDED 分发（默认 STOP 停机）；成本记录不加领域 outcome（可推导） |
| [stall-detection](../../../adr/domain/reasoning/2026-08-30-stall-detection.md) | 循环停滞检测经动作指纹（工具 + 规范化参数）+ 连续计数：超限走 STALLED 分发硬终止（默认 STOP 停机，不执行本轮工具）；result_hash 防轮询误判 / 周期检测为升级路径 |
| [reactor-max-execution-time](../../../adr/domain/reasoning/2026-08-27-reactor-max-execution-time.md) | 外层 timeout 到期触发 task 取消，内部 deadline 预留有界清理窗口；超时保留当前轮可见部分成果，领域终态组装是 scope 外无副作用尾部 |
| [reasoning-feedback](../../../adr/domain/reasoning/2026-08-27-reasoning-feedback.md) | reasoning_content 回喂策略（has_reasoning 覆盖空串，防 400） |
| [react-stream-channel](../../../adr/domain/reasoning/2026-09-04-react-stream-channel.md) | ReAct 同一 execute 循环换 LLM 通道（stream_mode）：流式 async_generate / 非流式 generate；失败契约对齐 + cancel 轮末补查（工业实证：OpenAI run/run_streamed 同循环、Claude include_partial_messages 选项） |
| [tool-error-feedback](../../../adr/domain/reasoning/2026-08-27-tool-error-feedback.md) | 工具失败回喂 `str(result)` + error/error_code 进证据链（模型自愈 + 根因可溯） |

---

## 问题记录

- [REASON-001 上下文预算仅工具路径生效](../../../issues/domain/reasoning/2026-08-30-context-budget-placement.md)：预算原放 `_handle_tool_calls` 尾部，非工具重试路径漏裁；已移主循环顶部统一裁剪（已修复）
- [REASON-002 UNKNOWN 部分进度](../../../issues/domain/reasoning/2026-08-30-unknown-partial-progress.md)：未捕获异常路径不保留部分进度，与其余终结护栏不一致；已统一按 current/last-visible 规则组装（已修复）
- [REASON-003 取消信号语义错位 + 未接线](../../../issues/domain/reasoning/2026-08-30-cancel-event-semantics.md)：优雅取消被误判为 LLM 失败 / 无调用方接线；已贯通 cancel_event 链路 + /chat/stop 真实实现（已修复）
- [REASON-004 协议异常 tool_calls 不一致](../../../issues/domain/reasoning/2026-08-30-protocol-error-empty-tool-calls.md)：finish_reason=tool_calls 信号与数据（空列表）/工具可用性（无工具）不一致，误入空输出重试 / 空转执行；已短路为 PARSE_FAILED 协议异常分发（已修复）
- [REASON-005 UNKNOWN error 脱敏](../../../issues/domain/reasoning/2026-08-30-unknown-error-redaction.md)：error 拼接完整异常文本泄漏内部细节；已改异常类型名 + 完整异常进日志（已修复）
- [REASON-007 LLM 失败重试上限](../../../issues/domain/reasoning/2026-08-31-llm-fail-retry-limit.md)：LLM 失败重试无独立上限（handler CONTINUE 可无限重试烧钱）；已补 max_llm_fail_retries 护栏（对齐空输出）（已修复）
- [REASON-008 空输出重试空消息污染](../../../issues/domain/reasoning/2026-08-31-empty-output-blank-assistant.md)：空输出重试轮向历史追加空 assistant 消息累积污染；已改纯空轮不追加（无产出不写历史）（已修复）
- [REASON-012 deadline 当前轮部分成果](../../../issues/domain/reasoning/2026-09-09-deadline-current-round-progress.md)：类型化 deadline 与硬超时统一保留当前轮可见成果和可得 usage（已修复）
- [REASON-013 deadline 清理窗口](../../../issues/domain/reasoning/2026-09-09-deadline-cleanup-grace.md)：内部协作式 deadline 提前触发，外层保留原硬超时作最终兜底（已修复）
- [REASON-014 内部 TimeoutError 分类](../../../issues/domain/reasoning/2026-09-09-internal-timeout-misclassified-as-deadline.md)：仅 timeout scope 实际到期才归 TIMEOUT；内部同名异常归 UNKNOWN 并保留当前轮成果（已修复）
- [REASON-015 续接上下文超限当前轮成果](../../../issues/domain/reasoning/2026-09-10-continuation-context-overflow-progress.md)：续接前缀被预算闸拒绝时保留当前内容与旧请求 usage，不再发起后续请求（已修复）
- [REASON-016 跨策略执行护栏](../../../issues/domain/reasoning/2026-09-10-cross-strategy-guard-priority.md)：类型化四类优先级、付费调用前后对称复查与成功结果先接管后终止（已修复）

---

## 相关文档

- [推理策略模块](reasoning.md)（主文档）
- [ReActAgent 桥接组件](../agent_doc/executor.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)
- [领域端口契约](../ports_doc/ports.md)（LLMGateway / ToolGateway / ContextBudgetPort / CostLimiterPort 契约）
- [ReAct 工业级对标基准](react_benchmark.md)（能力基准与差距清单）
- [领域层说明](../README.md)
