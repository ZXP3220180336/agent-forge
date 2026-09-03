# 半流中断接续策略决策（text-only prefix continuation）

> **状态**：✅ 已采纳（2026-09-03 实施，含单元/集成测试）
> **决策日期**：2026-09-03
> **涉及模块**：`app/integration/llm/streaming_rectifier.py`（整流循环拆 `_drain` + 半流续接链 `_try_continuations` / `_SeamStripper`）· `app/integration/llm/llm_service.py`（`continue_fn` 续接请求构造）· `app/config/settings.py` / `app/container.py`（`llm_stream_max_continuations` 接线）· `app/domain/ports/llm_gateway.py`（usage 口径说明）
> **关联文档**：[streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [config.md](../../../docs/config_doc/config.md) · [ADR-005（整流）](2026-08-01-streaming-rectification-retry.md)

---

## Context

主链路（Yield RCA 长文本根因报告）里，流式生成**已产出部分 token 后再断流**（半流中断）时，现状是**放弃**：部分 content 保留在 `StreamResult` + `result.error` 置位 + 产出「流式响应中断」SSE，ReAct 把该轮当 LLM 失败重跑整轮。代价：

- 已实时发给用户的部分文本作废（SSE token 不可撤回），整轮上下文重新生成 → 用户看到「前半段 + 另一段不一致答案」或重复整段；
- 整轮重跑重新计费（prompt 全量 + 全新 completion），半流文本白白浪费。

本决策为这类「服务端 ↔ provider 生成中途断」设计**接续**（不是从头整流）：带已产出 content 作前缀续写，让单轮输出连续收敛。**与 LLM-ADR-005「已产出 token 后不整流（从头重启）」不冲突**：整流 = 首 token 前整段重来（用户没看到输出，安全）；本决策是正交新路径——已产出后**带前缀续写**，不重启、不重放，直接消解 ADR-005 列出的三顾虑（重复内容 / 双倍计费 / tool_calls 残缺）——tool_calls 与 reasoning 场景直接排除在续接之外。

### 工业级参照（调研 gate 结论）

主流框架**没有传输层 token 续接**——恢复只发生在两层，且都与本设计正交或为同构：

| 参照 | 机制 | 与本设计关系 |
| --- | --- | --- |
| [openai/openai-agents-js #1592](https://github.com/openai/openai-agents-js/issues/1592) | **LLM prefix continuation**：把不完整 assistant 文本（`output-text-delta` 累积）作最后一条 assistant 消息续写；**范围仅纯文本**，tool call 中断丢弃重发全量；先决条件是 state 层先累积 partial + 标记 incomplete | 本设计直接采用同构手法 |
| DeepSeek / vLLM / Anthropic | `assistant` 消息 `prefix: true`（DeepSeek beta 续写）、vLLM `continue_final_message`、Anthropic prefill——**provider 扩展能力**，OpenAI 自家 Chat Completions 不保证（“可能续写也可能重复，靠运气”） | 本项目 provider 为 DeepSeek（OpenAI 兼容），`prefix:true` 是原生续写路径；非 prefix 端点失败时**尽力而为退化**，无害 |
| [Vercel AI SDK resumable stream](https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-resume-streams) | `resume:true` + Redis 缓冲 + `Last-Event-ID` 游标重放——解决**客户端↔服务端**断线重连（同一服务端生成继续跑，缓冲重放）；不解决服务端↔provider 生成中途断 | 不是本设计对象（传输游标续传是另一课题，记升级路径） |
| [LangChain `.with_retry()` / DeepAgents](https://github.com/langchain-ai/deepagents/pull/4569) | 流中途断只整体重启（`with_retry` 重试整个 stream setup 非 token）——DeepAgents 明示「已开始流到用户侧就不再重试防重复」 | 整体重启会重复可见输出 + 双烧——正是本设计要避免的 |
| ChatGPT “Continue generating” | 前端把部分文本留在历史 + 「继续不要重复」指令再请求，客户端拼接 | 同 prefix-continuation 变体；提示词去重不可靠，故本设计用 `prefix:true` + 接缝重叠剥离 |

**结论**：业界对服务端半流中断的生成层恢复统一走 **prefix continuation（纯文本）**；tool call 半成品 JSON 无法跨请求续接是社区共识。本设计照此落地，并针对「已产出 token 不可撤回」补接缝重叠剥离。

## Decision

**半流中断（已产出 content）且满足全部边界 → 携带 `result.content` 作 assistant 前缀续写（尽力而为）；续接失败一律退化到现有放弃路径，不劣化现状。**

### 1. 触发边界（全部满足才续接，`_should_continue`）

1. 中断发生在**已产出 content**（`result.content` 非空）之后，且 `finish_reason is None`（模型未收尾）、`refusal is None`；
2. 异常可恢复：`classify_error ∈ RETRYABLE / RATE_LIMITED`（与整流复用；RATE_LIMITED 尊重 Retry-After，封顶 `max_delay`）；
3. **reasoning 未产出**（`not has_reasoning` 且 `reasoning_content` 空）——DeepSeek thinking 带 tools 需回喂 `reasoning_content` 否则 400（[react.py 回喂约束](../../../app/domain/reasoning/react.py)）+ prefix 续写对 reasoning 半段语义未验证，保守排除；
4. **无 tool_call 半成品**（本次中断的 `tool_deltas` 空）——partial JSON 无法跨请求续接；
5. 未取消；续接轮次未超 `llm_stream_max_continuations`；编排层已提供 `continue_fn`。

任一不满足 → 维持现有放弃分支（部分 content 保留 + `result.error` 失败信号），行为与决策前一致。

### 2. 续接请求构造（编排层 `llm_service.async_generate`）

`continue_fn(prefix)`：`kwargs` 复制 → `messages_copy = list(原 messages) + [{"role": "assistant", "content": prefix, "prefix": True}]` → 走 `_rate_limited_call`（**重新 reserve**，与整流同「每次真实请求单独结算」语义）。**不改**调用方 `messages`（副本）。`prefix` = 中断瞬间 `result.content` 快照（续接链每次用最新累积值）。

### 3. 整流循环改造（`streaming_rectifier`）

- 迭代主体（首包/空闲看门狗 + chunk 累积/事件产出）抽为 **`_drain`**，整流 attempt 与续接 attempt 共用；
- except 分支顺序：**整流判定 → 续接判定 → 放弃**。续接链 `_try_continuations`：退避 → 清死流元数据（`finish_reason/usage/refusal=None`，**content 保留**）→ `continue_fn(result.content)` → `_drain` 迭代；create 失败（如非 prefix 端点拒字段）→ 记录日志后退化放弃（`result.error` 用**原中断原因**，对用户更贴切）；迭代再断且预算余 → 带新前缀再续；超预算 → 放弃（喂熔断 + 失败信号照旧）。
- **续接请求不经 `retry.execute` / fallback**——尽力而为单链（非主干路径），次数有界（`llm_stream_max_continuations`，默认 1）。
- **接缝重叠剥离** `_SeamStripper`：续接流首部若与已产 content 尾部重叠（窗口 ≤ `_SEAM_OVERLAP_LIMIT`=64 字符）剥离后再产出/累积——已发给客户端的不重复显示；流自然结束仍全命中重叠视为纯重放丢弃。前提 `prefix:true` 使长重复极罕见，剥离只兜小尾巴。

### 4. 配置

`settings.llm_stream_max_continuations: int = 1`（0=禁用），经 container 注入 `LLMService.register_config(continuation_max_retries=...)` → `async_generate` 传入整流器。独立于整流上限 `llm_stream_max_retries`（整流=首 token 前，续接=已产出后，故障阶段不同，需独立调优）。

### 5. usage / 成本口径

续接成功 → `result.usage` = **末次完成流（续接请求）的 usage**，与整流「末次成功流」同口径（[llm_gateway](../../../app/domain/ports/llm_gateway.py) docstring 已同步）；断流 attempt（attempt0 或中途再断）的消耗传输层**数据不可得 → 不计、不估**（[LLM-039](../../../issues/integration/llm/2026-09-02-usage-accounting.md) 口径）。react 侧每轮消费单个 `StreamResult.usage` 的累加语义不变。

## Consequences

- ✅ 半流单轮收敛：中断不整轮作废，用户看到「部分 + 续写」连续一段；ReAct 不再为半流答案重跑整轮。
- ✅ 不劣化：续接失败（provider 不支持 prefix / 连续断流 / 预算尽）退化放弃，保留部分 content + 失败信号，等同决策前行为；熔断 feeding 语义不变（最终放弃仍按 RETRYABLE 喂）。
- ⚠️ 成本：续接把已产 content 当前缀重发（近似一次全上下文 reprompt + 部分 completion）——次数有界（默认 ≤1），且仅在真实断流时发生；对比整轮重跑（全上下文 + 全新 completion）仍更省，且避免不一致答案。
- ⚠️ provider 依赖：`prefix:true` 是 DeepSeek/vLLM/Anthropic 类扩展能力，OpenAI 自家不保证——非 prefix 端点下续接 create 失败自动退化，无收益但不劣化。
- 📌 升级路径（按产品导向暂不实现）：tool_call 半成品续接（需 provider 支持分片工具续写）；reasoning 半段续写（先验证 DeepSeek prefix + thinking 回喂语义）；SSE 传输游标续传（Vercel resume 式，客户端↔服务端课题，独立决策）。
