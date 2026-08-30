# ReActStrategy 设计文档

> **模块**：`app/domain/reasoning/react.py`
> **更新日期**：2026-08-29
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
2. **可复用工具原语**：`execute_tool_calls()` 独立成原语（并行执行 + 保序），供 ReAct 循环自身与 PlannerAgent 执行阶段 / ReflectionAgent 收集阶段复用
3. **错误处理横切**：各终止 / 可恢复错误经 `ErrorHandlerRegistry` 按 kind 分发（CONTINUE / STOP / RAISE），默认行为 = 现有逻辑，调用方可注册覆盖
4. **结构化最终答案**：`output_schema` 启用时注入 final_answer 工具，模型最后调用提交 schema 约束结果并终止循环（兼作终止机制，无额外 LLM 调用）
5. **上下文预算护栏**：模型下次调用前经 `ContextBudgetPort` 裁剪（轮次 + token 双层），防上下文膨胀
6. **成本上限护栏**：每轮 usage 累加后经 `CostLimiterPort` 折算成本，超限走 `COST_EXCEEDED` 分发停机（默认 STOP 降级），防长任务费用失控
7. **纯算法依赖方向**：只依赖 ports + shared + 标准库，收标量参数（非 `AgentContext`）——可独立测试、可被任意编排复用

## 核心概念解释

### ReAct 循环（推理 → 行动 → 观察）

ReAct 是「推理 → 行动 → 观察」的循环：LLM 每轮推理产出 `finish_reason`，据此决定调用工具（行动）并回喂结果（观察）、正常结束或重试。`execute()` 承载主循环，各终止 / 错误分支拆分为职责单一的方法（`_finalize_*` / `_handle_*`），以 `outcome is not None` 作为终止信号。

### finish_reason 分支判定

LLM 单轮回复的 `finish_reason` 决定下一步：

| finish_reason | 语义 | 处理 |
| --- | --- | --- |
| `tool_calls` | 模型请求调用工具 | final_answer 检测 → 执行工具 → 结果回喂 → 下一轮 |
| `stop` / `length` | 正常生成完毕（length 为截断） | `_finalize_stop` 正常结束 |
| 空 + 无内容 | 模型未生成有效输出 | `_handle_empty_output` 错误分发（默认重试） |

### 错误处理分发（ErrorHandlerRegistry）

错误处理是共享内核横切能力（`app.shared.error_handling`）：按 `AgentErrorKind` 注册 handler，决策 `CONTINUE`（回喂继续）/ `STOP`（终止）/ `RAISE`（上抛 `AgentRunError`）。ReAct 循环内触发 9 类 kind，与 BaseAgent 的 `CANCELLED` / `UNKNOWN` 合计 11 类：

| `AgentErrorKind` | 触发场景 | 默认 action |
| --- | --- | --- |
| `LLM_FAILED` | LLM 调用失败（`StreamResult.error`） | STOP（短路失败） |
| `EMPTY_OUTPUT` | 空输出 | CONTINUE（重试，连续超过 `max_empty_retries` 硬终止） |
| `MAX_TURNS` | 迭代耗尽 | STOP（兜底） |
| `TIMEOUT` | 总时长超限 | STOP（降级） |
| `COST_EXCEEDED` | 累计成本超限 | STOP（停机降级） |
| `STALLED` | 连续相同工具调用（工具+参数） | STOP（停机） |
| `TOOL_FAILED` | 工具执行失败 | CONTINUE（回喂） |
| `PARSE_FAILED` | 工具参数 JSON 解析失败 | CONTINUE（回喂） |
| `STRUCTURED_INVALID` | final_answer 参数校验失败 | CONTINUE（回喂） |

### 成本上限（CostLimiterPort）

成本上限是横切护栏，由应用层 `CostLimiter` 结构实现（经 `CostLimiterPort` 注入；成本估算经 `LLMGateway.calculate_cost` 取——成本估算是 LLM 能力，应用层不直接依赖集成层），ReAct 不实现算法。每轮 usage 累加后 `check(累计 usage)` 折算成本（USD），超限即 `_finalize_cost_exceeded` 走 `COST_EXCEEDED` 分发（默认 STOP 降级，error 记录「成本超限（累计 $X）」）。置于 error 判断前：预算超限时不允许失败重试 / 工具执行再产生付费调用或副作用。`cost_limiter=None`（未配置 `agent_max_cost`）整段零开销。与上下文预算互补：trim 在 LLM 调用前（减少发送 token），cost check 在调用后（审计花费）。

### 循环停滞检测（动作指纹 + STALLED）

死循环护栏：模型「反复调用同一工具同一参数」原地打转时，靠 `max_same_action_turns`（默认 3）主动停机——连续相同工具调用（工具+参数）超过上限走 `STALLED` 分发硬终止（默认 STOP），**不执行本轮工具**（防重复副作用 / 烧钱）。动作指纹 = 本轮 tool_calls 的（名, 规范化参数）序列化：参数 `json.loads` 后 `sort_keys=True` 重 dump（key 顺序 / 空白不同指纹一致），非法 JSON 回退原始串；换工具 / 换参数重置计数；`final_answer` 不参与（终止工具）；STALLED handler 可 RAISE 上抛（对齐 COST_EXCEEDED 终结护栏）。

### 多工具失败聚合与仲裁

同轮多个工具失败的处理意图可能不同（上报 / 终止 / 回喂）——先按 kind 分组聚合（同 kind 失败原因合并为一条 message 给 handler），再逐 kind 分发，最后按**最严重优先**仲裁（`RAISE > STOP > CONTINUE`）。终止 / 上报时其他失败不回喂（循环结束，回喂无意义），但全部失败已进证据链记录。

### 结构化最终答案（final_answer 工具）

`output_schema` 启用时，注入 `final_answer` 工具（参数 = schema）。模型完成任务后调用一次提交结构化结果，`_handle_final_answer` 解析 + jsonschema 校验：成功即终止循环并写入 `outcome.structured`；校验失败走 `STRUCTURED_INVALID` 分发（默认回喂错误文本，模型下轮自纠）。注入工具非注册工具，识别在主循环，不经过 `execute_tool_calls`。

### 上下文预算（ContextBudgetPort）

上下文预算是横切护栏，由应用层 `context_manager` 统一实现（经 `ContextBudgetPort` 注入），ReAct 不实现算法。置于**循环顶部、每次 LLM 调用前裁剪**（所有继续路径共用：工具回喂 / LLM 失败重试 / final_answer 回喂重试 / 空输出重试——否则非工具路径上下文无限增长、预算失效）：保留 system/user 前缀 + 最近 N 轮 assistant/tool 配对（`max_context_rounds`）+ token 硬上限（`max_context_tokens`）。选 trimming 而非摘要——摘要压缩工具原始记录会破坏证据链（产品核心）。

### 工具结果回喂与截断

工具结果回喂模型时：成功回喂 `content`，失败回喂 `str(result)`（`"错误: <error>"`，见 `ToolResult.__str__`）——模型需看到失败原因才能自愈。回喂文本截断：tool 消息 2000 字符、tool_result 事件 200 字符，截断处追加 `[结果已截断]` 标记（预留标记长度，总长不超限）——模型可知结果不完整，可缩小范围重查。

### reasoning_content 回喂

DeepSeek V4 thinking 模式带 tools 时必须回喂 `reasoning_content`（否则 400）。`StreamResult.has_reasoning` 区分「未返回」与「返回空」：未返回不回喂（chat 模型），返回空也回喂空串（thinking 模型）——字段始终存在。

## 架构总览

```text
ReActAgent._strategy_cycle()（agent/ 层编排：生命周期 / 状态 / 结果组装）
        ▼ execute()
ReActStrategy.execute()（ReAct 主循环）
    ├── ContextBudgetPort.trim_messages ──► 上下文预算（模型调用前裁剪）
    ├── CostLimiterPort.check ────────────► 成本上限（usage 累加后折算成本，超限停机）
    ├── LLMGateway.async_generate ────────► LLM 推理（流式 reasoning/message 事件）
    ├── ErrorHandlerRegistry（shared）─────► 10 类 AgentErrorKind 分发（CONTINUE/STOP/RAISE）
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

## 组件详解

### 终止分支

| 方法 | 触发 | 默认行为 |
| --- | --- | --- |
| `_finalize_stop` | finish_reason=stop/length/有内容 | 正常结束：`outcome.success = content 非空` |
| `_finalize_llm_failed` | `StreamResult.error` 非空 | STOP 短路失败（`success=False` + error）；handler 可 CONTINUE 重试 / RAISE 上抛 |
| `_finalize_max_turns` | 循环达到 `max_iterations` | STOP 兜底：用 `last_result` 组装 outcome；无结果则 `error="LLM 未返回任何结果"` |
| `_finalize_timeout` | `asyncio.timeout(max_execution_time)` 触发 | STOP 降级：用 `last_result` 组装（有 content 算部分成功），error 记录超时 |
| `_finalize_cost_exceeded` | `CostLimiterPort.check(累计 usage)` 超限 | STOP 停机：用 `last_result` 组装（有 content 算部分成功），error 记录「成本超限（累计 $X）」 |
| `_finalize_stalled` | 连续相同工具调用超过 `max_same_action_turns` | STOP 停机：组装 outcome（content 空，保留 reasoning），error 记录「连续 N 轮相同工具调用」；本轮工具不执行 |

关键语义：`asyncio.timeout` 包整个循环实现「总时长上限」（非单轮预算）；超时降级判别「真超时 vs 生成器被 finalizer 关闭」（慢消费者场景 aclose 由不同 task 驱动），后者干净停止、不 yield 降级事件（避免 `RuntimeError: async generator ignored GeneratorExit`）。

### 可恢复分支

| 方法 | 触发 | 默认行为 |
| --- | --- | --- |
| `_handle_empty_output` | finish_reason 空 + content 空 | CONTINUE 重试；连续超过 `max_empty_retries` 硬终止（handler 可 STOP / RAISE，CONTINUE 被忽略） |
| `_handle_tool_calls` | finish_reason=tool_calls | 调 `execute_tool_calls` 执行 → 失败工具按 kind 聚合分发 + 仲裁 → 全 CONTINUE → 继续循环（预算在循环顶部统一裁剪，见上下文预算节） |
| `_handle_final_answer` | 检测到 final_answer 工具调用 | 成功提取 → 终止写 `outcome.structured`；校验失败 → `STRUCTURED_INVALID` 分发（默认回喂自纠） |

### 支撑方法（_finalize_outcome / _finalize_terminal / _dispatch）

- `_finalize_outcome(*, success, content, reasoning, iteration, total_usage, error, info_message, structured) -> AsyncGenerator[str]`：统一收尾——组装 outcome + 产出事件（可选 info + done 恰一次），供各终结 / STOP 分支复用（dispatch 由调用方负责——CONTINUE 语义各异：重试 / 回喂 / 忽略）
- `_finalize_terminal(kind, message, iteration, *, success, content, reasoning, total_usage, error, info_message, structured) -> AsyncGenerator[str]`：终结性护栏统一收尾——dispatch（RAISE 上抛，CONTINUE 忽略）→ 复用 `_finalize_outcome`；供 TIMEOUT / COST_EXCEEDED / STALLED / MAX_TURNS 复用
- `_dispatch(kind, message, iteration) -> AgentErrorAction`：错误分发唯一入口——`RAISE` 决策抛 `AgentRunError`，否则返回 action（统一 9 处分发点）

### 工具并行原语（execute_tool_calls）

```python
async def execute_tool_calls(self, tool_calls: list[dict], messages: list[dict], iteration: int) -> AsyncGenerator[str]:
```

`asyncio.gather` 并行执行所有工具（并发度由 ToolService 信号量 `agent_max_concurrent_tools` 限制），gather 保证结果顺序 = 输入顺序——OpenAI 兼容 API 要求 tool 消息与前置 assistant.tool_calls 的 `tool_call_id` 配对，顺序不能乱。并发 task 内只做执行不 yield 事件（避免事件交错）；SSE 事件只在主 generator 内按序 yield。工具参数 JSON 解析失败不静默用空参执行（会掩盖错误 / 可能触发副作用），构造失败 `ToolResult`（JSON_PARSE）走失败回喂。

### ReActOutcome（结果载体）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `content` | `str` | 最终回答 |
| `reasoning` | `str` | 完整推理过程（累计） |
| `structured` | `dict \| None` | 结构化最终答案（final_answer 产出，output_schema 启用时） |
| `tool_calls` | `list[dict]` | 工具调用记录（tool/params/result/success/error/error_code/duration） |
| `iterations` | `int` | 实际轮数 |
| `total_tokens` / `usage` | `int` / `dict \| None` | Token 统计（累计） |
| `error` | `str \| None` | 失败原因（LLM 调用失败 / 无结果 / 超时 / 按策略终止） |
| `success` | `bool` | 是否成功 |

桥接方（`ReActAgent._map_outcome`）将 `ReActOutcome` 组装为 `AgentResult`。

## 执行流程

```text
第 N 轮推理开始（for iteration in 1..max_iterations）
  ├─ 0. 上下文预算：ContextBudgetPort.trim_messages（每次 LLM 调用前裁剪，所有继续路径共用）
  ├─ 1. LLM 推理：async_generate 流式，yield reasoning/message 事件，累计 usage
  ├─ 2. 成本护栏：CostLimiterPort.check(累计 usage) 超限？→ _finalize_cost_exceeded
  │       （默认 STOP 降级：error 记录「成本超限（累计 $X）」；置 error 判断前——
  │         预算超限时不允许失败重试 / 工具执行再产生付费调用或副作用）
  ├─ 3. stream_result.error 非空？→ _finalize_llm_failed
  │       STOP（默认）→ 短路失败；CONTINUE → 重试下一轮
  ├─ 4. 追加 assistant 消息（reasoning_content 按 has_reasoning 回喂 + tool_calls 配对，防 400）
  ├─ 4.5 空输出连续计数：本轮有产出（工具调用/stop/length/有内容）→ 清零；空输出 → +1
  ├─ 5. finish_reason 分支：
  │     ├─ "tool_calls" 且有工具 →
  │     │     ├─ 含 final_answer？→ _handle_final_answer（成功终止 / 校验失败回喂）
  │     │     ├─ 停滞检测：连续相同工具调用超 max_same_action_turns → _finalize_stalled（不执行工具）
  │     │     └─ _handle_tool_calls：execute_tool_calls 并行执行 → 失败工具按 kind 聚合分发 + 仲裁
  │     │           → 全 CONTINUE → 下一轮
  │     ├─ "stop"/"length"/有内容 → _finalize_stop（正常结束）
  │     └─ 空输出 → _handle_empty_output（默认 CONTINUE 重试；连续超 max_empty_retries 硬终止；STOP → 终止）
  ├─ 6. 循环耗尽 → _finalize_max_turns（默认 STOP：last_result 兜底）
  └─ 7. asyncio.timeout(max_execution_time) 触发 → _finalize_timeout（默认 STOP 降级）
```

## 对外接口

> 仅列模块对外暴露的公共接口（被 agent/ 层编排或调用方依赖）；`_` 前缀私有方法（终止 / 可恢复分支）见组件详解。

| 方法 | 同步/异步 | 说明 |
| --- | --- | --- |
| `__init__(llm, tools, context_budget=None, error_handlers=None, cost_limiter=None)` | 构造 | 注入端口依赖（LLMGateway / ToolGateway）+ 横切能力（ContextBudgetPort / ErrorHandlerRegistry / CostLimiterPort） |
| `execute(user_input, messages, *, max_iterations, temperature, max_tokens, max_execution_time=None, max_context_rounds=None, max_context_tokens=None, max_empty_retries=2, output_schema=None) -> AsyncGenerator[str]` | 异步生成器 | ReAct 主循环；yield SSE 事件（reasoning/message/tool_call/tool_result/info/done），结果写入 `outcome` |
| `execute_tool_calls(tool_calls, messages, iteration) -> AsyncGenerator[str]` | 异步生成器 | 工具并行执行原语（gather 保序 + 事件产出 + 记录）；独立使用场景：PlannerAgent 执行阶段 / ReflectionAgent 收集阶段 |
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

## 边界情况

1. **LLM 调用失败**（`StreamResult.error` 非空）→ `_finalize_llm_failed`，默认 STOP 短路 `success=False` + error；不把「失败」当「空输出」空转重试（浪费 LLM 调用 + 错误信息不准确）
2. **空输出**（finish_reason 空 + content 空）→ `_handle_empty_output`，默认 CONTINUE 重试下一轮
3. **达到 `max_iterations`** → `_finalize_max_turns`，用 `last_result` 兜底强制结束；无结果则 `error="LLM 未返回任何结果"`
4. **达到 `max_execution_time`**（None=不设限）→ `_finalize_timeout`，用 `last_result` 降级（有 content 算部分成功），error 记录超时；慢消费者关闭生成器时干净停止、不 yield 降级事件
5. **累计成本超限**（`agent_max_cost`，None=不启用）→ usage 累加后经注入的 CostLimiterPort 折算成本，超限走 `COST_EXCEEDED` 分发（默认 STOP 降级：error 记录「成本超限（累计 $X）」）；`cost_limiter=None` 零开销
6. **工具参数 JSON 解析失败** → 不执行工具：构造失败 ToolResult（JSON_PARSE）回喂模型自纠，`error`/`error_code` 进证据链
7. **工具执行失败 / 无效工具名** → 回喂 `str(result)`（`"错误: <error>"`，无效工具含「未注册」），模型可感知失败自愈；`error` / `error_code` 进证据链
8. **工具结果超长** → 截断（tool 消息 2000 字符 / 事件 200 字符）并追加 `[结果已截断]` 标记（预留标记长度，总长不超限）
9. **reasoning_content 回喂** → DeepSeek V4 thinking + tools 必须回喂（否则 400）；`has_reasoning` 覆盖空 reasoning（空串也回喂），无信号不回喂（chat 模型）
10. **上下文预算**（`max_context_rounds` / `max_context_tokens`，None=不裁剪）→ 循环顶部、每次 LLM 调用前经注入的 ContextBudgetPort 裁剪（所有继续路径共用）：保留最近 N 轮 assistant/tool 配对 + token 硬上限
11. **结构化最终答案**（`output_schema`，None=不启用）→ 注入 final_answer 工具；模型调用即终止产出 `outcome.structured`；参数校验失败回喂（VALIDATION/STRUCTURED_INVALID）自纠
12. **错误处理分发**（`error_handlers`，None=默认行为）→ 各终结/可恢复错误按 kind 分发（CONTINUE/STOP/RAISE）；默认 = 现有行为，调用方按 kind 注册覆盖
13. **多工具失败** → 按 kind 聚合（同 kind 原因合并给 handler）+ 最严重优先仲裁（RAISE > STOP > CONTINUE）；终止/上报时其他失败不回喂，但全部失败已进证据链
14. **空输出重试上限**（`max_empty_retries`，默认 2）→ 连续空输出计数，超过上限在空输出分支硬终止（`error` 记录「连续空输出（N 轮）」，先 dispatch 供 handler RAISE，CONTINUE 忽略）；有产出轮计数清零（非连续不累计）；LLM 失败重试轮不参与
15. **循环停滞检测**（`max_same_action_turns`，默认 3）→ 连续相同工具调用（工具+参数）超过上限 → STALLED 分发硬终止（本轮工具不执行，error 记录「连续 N 轮相同工具调用」）；参数规范化（key 顺序 / 空白不同指纹一致）；换工具 / 换参数重置；`final_answer` 不参与；STALLED handler 可 RAISE 上抛

## 配置项清单

配置经装配根注入 `AgentContext` → `ReActStrategy.execute()`（生产值覆盖，字段默认 None 向后兼容）：

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `agent_max_iterations` | int | 10 | `max_iterations` 生产值（迭代上限） |
| `agent_timeout` | int | 300 | `max_execution_time` 生产值（循环总时长上限，秒） |
| `agent_max_context_rounds` | int | 8 | `max_context_rounds` 生产值（上下文预算保留轮数） |
| `agent_max_cost` | float \| None | None | 成本上限（美元 USD）；None=不启用（装配根据此构造 CostLimiter 注入，0 则任何正成本即停） |
| `agent_max_empty_retries` | int | 2 | 连续空输出重试上限：空输出最多重试 N 次，第 N+1 次仍空输出则终止（0=首次空输出即终止） |
| `agent_max_same_action_turns` | int | 3 | 循环停滞检测：连续相同工具调用（工具+参数）超过 N 轮，下一轮仍相同则 STALLED 终止 |

`max_context_tokens` 无独立配置，由装配根直接注入。完整配置表见 [config 文档](../../config_doc/config.md)。

## 测试状态

`tests/unit/test_react_strategy.py`（50 用例）覆盖分类：

- **工具循环**：stop 结束（outcome 组装）/ 空输出重试后结束 / 持续空输出 → 迭代兜底
- **工具原语**：并行保序（延迟交错，结果顺序 = 输入顺序）/ 实际并发（总耗时 < 串行和）
- **时间上限**：首轮超时降级 / 中途超时保留部分进度 / 宽松上限不影响完成 / `None` 显式不设限
- **成本上限**：首轮超限 STOP 降级 / 中途超限保留部分进度 / 宽松上限不触发 / `cost_limiter=None` 不启用 / COST_EXCEEDED→RAISE 上抛 / CONTINUE 忽略
- **空输出重试上限**：持续空输出达上限终止 / 恰好达上限仍重试 / 上限可配置 / 有产出后计数重置 / 达上限 handler RAISE 上抛
- **循环停滞检测**：同工具同参数达上限终止 / 未达上限正常 / 参数变化重置 / 换工具重置 / 上限可配置 / final_answer 不参与 / STALLED handler RAISE / 参数 key 顺序规范化指纹相同
- **工具失败**：失败回喂 / 证据链记录 error+error_code / 无效工具名 NOT_REGISTERED / 解析失败不执行工具 + JSON_PARSE / 截断标记（带标记不超限 / 短结果无标记）
- **reasoning 回喂**：`has_reasoning` 回喂空串 / 无信号不回喂
- **上下文预算**：注入 ContextBudgetPort 后轮次裁剪生效 / 非工具路径（空输出重试）每次 LLM 调用前也裁剪
- **final_answer**：成功提取终止 / 校验失败回喂 / 未配置不注入
- **错误处理**：LLM_FAILED→CONTINUE 重试 / LLM_FAILED→RAISE 上抛 / EMPTY_OUTPUT→STOP / TOOL_FAILED→STOP（部分进度保留）/ STRUCTURED_INVALID→STOP
- **多工具失败**：同 kind 聚合 message / 跨 kind STOP 仲裁 / 跨 kind RAISE 仲裁

另经 `tests/unit/test_agent.py`（6 用例）间接覆盖（`ReActAgent` 编排路径 + cost_limiter 透传，见 [executor.md](../agent_doc/executor.md)）。

## 设计决策

| ADR | 一句话结论 |
| --- | --- |
| [react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md) | ReAct 算法从 agent/ 抽离至 reasoning/（原子策略），`execute_tool_calls` 原语化 |
| [agent-error-handling](../../../adr/domain/agent/2026-08-28-agent-error-handling.md) | 错误处理横切 ErrorHandlerRegistry，按 kind 分发（可恢复默认回喂、终结性默认终止） |
| [structured-output](../../../adr/domain/reasoning/2026-08-28-structured-output.md) | 结构化用 Final Answer 工具（模型原生，兼作终止）而非事后提取；`generate_structured` 留非 Agent 场景 |
| [context-budget](../../../adr/domain/reasoning/2026-08-28-context-budget.md) | 上下文预算归 context_manager（横切），经 ContextBudgetPort 注入；选 trimming 而非摘要（保证据链） |
| [cost-limit](../../../adr/domain/reasoning/2026-08-30-cost-limit.md) | 成本上限经 CostLimiterPort 注入（应用层经 LLMGateway.calculate_cost 取成本估算），超限走 COST_EXCEEDED 分发（默认 STOP 停机）；成本记录不加领域 outcome（可推导） |
| [stall-detection](../../../adr/domain/reasoning/2026-08-30-stall-detection.md) | 循环停滞检测经动作指纹（工具 + 规范化参数）+ 连续计数：超限走 STALLED 分发硬终止（默认 STOP 停机，不执行本轮工具）；result_hash 防轮询误判 / 周期检测为升级路径 |
| [reactor-max-execution-time](../../../adr/domain/reasoning/2026-08-27-reactor-max-execution-time.md) | `asyncio.timeout` 包循环实现总时长上限，超时对齐 max_iterations 兜底降级 |
| [reasoning-feedback](../../../adr/domain/reasoning/2026-08-27-reasoning-feedback.md) | reasoning_content 回喂策略（has_reasoning 覆盖空串，防 400） |
| [tool-error-feedback](../../../adr/domain/reasoning/2026-08-27-tool-error-feedback.md) | 工具失败回喂 `str(result)` + error/error_code 进证据链（模型自愈 + 根因可溯） |

## 问题记录

- [AGENT-001 except 逗号语法回归](../../../issues/domain/agent/2026-08-17-except-comma-tuple-semantics.md)：`except (A, B)` 与 `except A, B` 语义差异导致的历史回归（已修复，回归护栏在测试）
- [REASON-001 上下文预算仅工具路径生效](../../../issues/domain/reasoning/2026-08-30-context-budget-placement.md)：预算原放 `_handle_tool_calls` 尾部，非工具重试路径漏裁；已移主循环顶部统一裁剪（已修复）

## 相关文档

- [推理策略模块](reasoning.md)（主文档）
- [ReActAgent 桥接组件](../agent_doc/executor.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)
- [领域端口契约](../ports_doc/ports.md)（LLMGateway / ToolGateway / ContextBudgetPort / CostLimiterPort 契约）
- [ReAct 工业级对标基准](react_benchmark.md)（能力基准与差距清单）
- [领域层说明](../README.md)
