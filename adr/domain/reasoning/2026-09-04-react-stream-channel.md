# ReAct 流式/非流式 LLM 通道切换（stream_mode）

> 日期：2026-09-04 ｜ 层级：domain/reasoning + domain/agent

## Context

- Phase C 主链路「主 Agent 拆分 → 并行子 Agent 排查」：后台子 Agent **无人订阅逐 token SSE**，只需最终产物。若 ReAct 硬编码流式 `async_generate`，后台场景白白承载流式协议开销；且 DeepSeek + 工具 + streaming 存在真实 400 bug（工业实证），工具型子 Agent 切非流式是稳妥路径。
- **工业级兼容机制调研**（2026-09-04，五框架详查 + DeepSeek 实证）见下文「工业级参照」节——共识：流式/非流式不是两套代码，是**单一执行循环 + 事件粒度/通道选项**，与本设计逐点对齐。
- 项目已具双通道（`LLMGateway.async_generate` / `generate`），最终产物同为 `StreamResult` → execute 主循环分支逻辑（回喂/拒答/finish_reason/工具/护栏）可全复用，只换 LLM 调用点。

## 工业级参照（2026-09-04 调研详情）

> 调研范围：主流 Agent 框架如何**在架构层兼容流式与非流式调用**（API 形态 + 内部机制 + 事件/结果契约），定稿前据用户要求逐框架查证。结论直接支撑本 ADR 决策。

### OpenAI Agents SDK（Python，`agents` 包）

- **API 形态**：`Runner.run()`（async 非流式，返回 `RunResult`）· `run_sync()`（同步壳，内部就是 `.run()`）· `run_streamed()`（返回 `RunResultStreaming`，LLM 走流式模式并实时吐事件）。三种入口的 **Agent 定义完全一致**（"The agent definition is identical whether using streaming or non-streaming execution"）。
- **底层机制 = 同一 agent loop**：官方 running_agents 文档明示三者跑同一循环（调 LLM → 判 final_output 结束 / handoff 换 agent / tool_calls 执行后重跑），原话 *"streaming uses the same agent loop and the same state strategies. The only difference is that your app consumes events while the run is happening."*——**流式与非流式不是两套执行，是"事件是否被订阅"**。
- **流式结果对象与非流式同接口**：`run_streamed()` 返回的 `RunResultStreaming` 在 `stream_events()` 迭代结束后，可访问 `final_output` / `new_items` 等——与非流式 `RunResult` 一致。即「流式跑完聚合 = 非流式结果」由对象同一接口承载。
- **每轮结构标记取法**：工具调用 / 消息等高级别项从 `result.new_items`（RunItem 列表）取，**不依赖事件流**（raw_response_event 是给逐 token 渲染用的低层流）。

### LangChain / LangGraph（Runnable 统一接口）

- **API 形态**：同一 Runnable 上并存 `invoke`/`ainvoke`（单入转单出）、`batch`/`abatch`、`stream`/`astream`（流式 chunk）+ `transform` 族。
- **底层机制 = 同核心 + 流式 override**：langchain_core Runnable 参考文档明示**默认 `stream` 实现就是调 `invoke`**、`astream` 默认调 `ainvoke`——「非流式」是基础原语；支持流式的子类才 override 以产出 chunk。方向与 OpenAI 相反（以 invoke 为根、stream 为增值），结论相同：**同一执行核心，两种消费面**。
- **架构推荐**：LangGraph 图节点内 `model.invoke()` **非流式**拿完整消息；流式只在**图边界** `graph.stream()` 按 `stream_mode`（values/updates/messages）发各节点状态。工具内嵌套 LLM 若想流式反而要抑制内部 token（`get_stream_writer` 只发自己的事件）→ **内部子任务非流式是工业常态**。

### Claude Agent SDK（Anthropic）

- **API 形态**：`query()` 恒为 **async iterator**（消息流）；`ClaudeSDKClient` 维持长会话。SDK 区分两种「流式」：partial-message 流（逐 token 增量）与 streaming-input（长进程喂消息），不要混淆。
- **兼容机制 = 同一事件流 + 粒度选项**：`include_partial_messages` 选项——默认 `False` 只 yield **完整消息**（宏观事件流），`True` 额外 yield `text_delta` partial。**非流式 ≈ 关闭 partial 粒度的同一条流**；纯结果用法 = 迭代流至终（不消费中间消息即可）。
- 与本方案 **execute 参数模式直接同构**：同一执行面，开关切事件粒度，而非两套 API。

### Vercel AI SDK（TypeScript）

- **API 形态**：`generateText`（非流式，官方定位：非交互场景 / 用工具的 agent / 批量）vs `streamText`（流式，UI 打字机，**带背压**——只生成被请求的 token，须消费流才完成）。
- **结构化双形态**：`generateObject`（非流式，schema 校验）vs `streamObject`/`Output.object`（partial 流式）；官方明示 **partial 输出无法对完整 schema 校验**（*"Partial outputs streamed cannot be validated against your schema, as incomplete data may not yet conform"*）→ **结构化/提取场景工业默认非流式**单列。
- **单 Agent 双出口**：Agent 类封装同一配置，暴露 `.generate()`（内部 `generateText`）与 `.stream()`（内部 `streamText`）——同一 agent 定义与执行面，仅出口不同。

### DeepSeek + 工具型 agent 实证（直接支撑本方案）

- 社区 + 工程教程实证：DeepSeek 兼容端上 `streaming + @function_tool` 会触发 **400**（OpenAI SDK 兼容缺陷）；工业采纳路径 = **工具型 agent 切非流式 `Runner.run()`**，规避 bug、保住成本面。
- 代价与补偿：失去 token-by-token 输出；每轮工具 / 交接标记**改从 `result.new_items`（结果对象）取**，不从事件流。
- 启示：本项目 Phase C 后台工具型子 Agent 走非流式，既规避 DeepSeek streaming+tool 400，又省去无订阅者的流式开销；工具证据链从 `messages` tool 消息 + `outcome.tool_calls` 取——**等价 `new_items` 语义**。

### 五框架共识提炼 → 本设计映射

| 工业共识 | 本设计落地 |
| --- | --- |
| 单一执行循环/状态机，非流式 = 流式执行不消费事件或整流聚合 | execute 单循环，`stream_mode` 只换 LLM 调用点（Decision #1） |
| Agent 定义不因流式与否而变；出口/选项切换（run/run_streamed、invoke/stream、generateText/streamText、include_partial_messages） | execute 单入口 + keyword-only `stream_mode` 参数（比工业双入口更收敛） |
| Agent 层对外恒为可迭代事件流，即使底层 LLM 非流式 | execute 保持 async generator；非流式轮合成整条 reasoning/message 事件（Decision #2） |
| 每轮结构标记从结果对象取，不依赖事件流（new_items） | `outcome.tool_calls` + messages tool 消息（既有证据链） |
| 结构化/工具调用场景工业默认非流式 | `generate_structured`（自查/修正/规划/汇总）保持非流式不动，本次只补 ReAct 主通道 |

## Decision

1. **单循环换通道**：`ReActStrategy.execute` 新增 keyword-only `stream_mode: bool = True`；`AgentContext` 加同名字段，`ReActAgent._strategy_cycle` 透传。`True`=流式 `async_generate`（默认，逐 token 事件，chat SSE 订阅者）；`False`=非流式 `generate(model_key="main")` 一次拿完整 StreamResult。
2. **事件合成（非流式轮）**：成功轮按 `reasoning_content`（先）→ `content`（后）合成整条 `reasoning`/`message` SSE 事件（复用 shared/events builder，与流式整流逐 token 事件逐字节同构；空串不产事件对齐整流）；失败轮合成一条 `error` 事件。流式路径不新增任何 yield → 零行为差异。
3. **失败契约映射（对齐流式整流可观测语义）**：`generate()` 返回 None（可恢复错误重试耗尽）→ 置 `stream_result.error` + error 事件 → 走 `LLM_FAILED` 分发（不误判空输出）；抛 `AppError` → 同折 `LLM_FAILED`（**流式路径此情形从不走 UNKNOWN**——整流器把 create 失败吞转 result.error）。共享 `AppError` 树含 LLMAPIError（4xx/认证/校验，集成层归一上抛）**与熔断 `CircuitBreakerOpenError`**（NonRetryableError 子类）→ 熔断亦被本分支捕获归 `LLM_FAILED`（与流式整流一致）；仅 `AppError` 树外的编程错误原样冒泡 → 外层 `except` → `UNKNOWN`。
4. **cancel 取舍**：`generate()` 无 cancel_event、无 chunk 级中断 → 非流式轮末补查一次 cancel（对齐 OpenAI after_turn 轮次边界），仅 `stream_mode=False` 生效；流式由整流层 chunk 边界中断覆盖。
5. **零配置化（YAGNI）**：不引入 settings/container 项；Phase C 子 Agent 自建 ctx 置 False。显式区分**命名撞车**：`settings.agent_streaming`（settings.py:157）仅作 `agent_config` 元数据出口、无行为接线，勿误接为新开关（届时才考虑复用）。

## Consequences

- ✅ 向后兼容：默认 True 行为与改动前完全一致（全量 799 passed 零回归，含既有 react 78 用例 / Reflection / chat_flow）；Reflection `_react.execute` 不传参数保持流式。
- ✅ 产品导向：Phase C 后台子 Agent 非流式路径就绪；工具标记从 `messages` tool 消息 + `outcome.tool_calls`（证据链）取——等价 OpenAI `RunResult.new_items` 语义，不依赖事件流。
- ✅ 新测试：`tests/unit/test_react_strategy_nonstream.py` 11 用例（合成事件 + model_key 断言 / 工具循环 / None→LLM_FAILED / cancel 轮末补查 / AppError→LLM_FAILED / 非 AppError→UNKNOWN / usage 累计 / 空输出恢复 / reasoning-only / final_answer / 双通道参数化）；test_agent 透传哨兵用例。
- ⚠️ **语义损失（知情取舍）**——三条均为「后台无人值守场景用非流式」主动付出的代价，每条都有场景合理性 + 既有补偿：
  - **整条一次性事件**：非流式轮 reasoning/message 整条合成，无逐 token 打字机效果——服务对象是无前端订阅的后台子 Agent（Phase C），无人实时观看；要给人实时展示则保持流式。
  - **cancel 仅轮次边界**：`generate()` 无 chunk 级中断，调用中无法打断（最多等当前轮生成完才在轮末补查停止）；轮与轮之间仍及时；实时聊天要即时打断则保持流式。
  - **可恢复失败细节丢失**：`generate()` 可恢复错误重试耗尽只返回 `None`（签名无失败原因载体），ReAct 只能给稳定文案「非流式调用可恢复错误重试耗尽」；真实原因已由可靠性层记入 `llm_call` 事件（产品侧稳定文案、观测侧诊断，对齐 REASON-005 脱敏原则）；上层若需拿到具体原因，扩 `generate()` result 回填参数即可（YAGNI 暂不做）。
- 📌 关联：本 ADR 使 [reasoning-feedback](2026-08-27-reasoning-feedback.md) Decision #3「非流式路径不处理」失效，已同步更新。
