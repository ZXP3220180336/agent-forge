# Provider 请求上下文准入

> 状态：✅ 已采纳（2026-09-06 修订 Decision 7，新增 Decision 9；2026-09-08 补充 Decision 8 structured 兑现）
> 日期：2026-09-06 ｜ 层级：integration（实现 domain `LLMGateway` 端口）
> 关联决策：[语义上下文预算](../../domain/reasoning/2026-08-28-context-budget.md)

## Context

`ContextManager` 的职责是决定消息历史中哪些语义可以裁剪或摘要。最终 provider 请求还包含 tools、`response_format`、结构化输出回喂、图片或文件、供应商协议开销，以及本次 `max_tokens` 或 thinking 预留；候选 messages 的预算不能证明该请求可被模型接受。

工业级参照：OpenHands 把 Bedrock 的共享输入/输出窗口规则和安全余量置于 LLM adapter；Amazon Bedrock 对 Claude 3.7/4 在 `prompt tokens + max_tokens` 超过窗口时返回校验错误，并将 thinking 与 tool use 纳入有效窗口计算。LangChain 将模型级 token 计数放在 Chat Model 接口，且明确通用计数未必覆盖 tools schema。

这要求请求准入位于拥有 provider payload 和模型能力配置的 Integration，而不是 Application 的 `ContextManager`。

## Decision

1. 保留 `ContextBudgetPort` 和 `ContextManager` 的职责：仅管理 Agent 多轮历史的可语义裁剪，不承担 provider 级请求序列化或 schema/tool 计数。
2. 在 Integration LLM 模块新增无状态请求预算闸，由 `LLMService` 在每个实际 provider 调用前执行。它接收最终 `model_key`、messages、tools、`response_format` 和本次 `max_tokens`，以当前模型编码器对完整请求负载做保守计数。
3. provider 可用输入额度定义为：

   ```text
   model_context_window_tokens - requested_max_tokens - safety_margin_tokens
   ```

   模型窗口和安全余量按 `main` / `reasoning` / `fast` / `fallback` 配置注入，禁止由模型名称硬编码推断。fallback 备用链路走独立 `fallback` 键窗口（见 Decision 9）。
4. 预算闸计入 messages、tool definitions、response format 和固定协议开销；它是客户端保守闸门，provider 的实际 tokenizer 仍是最终事实源。未知模型回退编码器时，安全余量必须生效，且日志只记录 token 数、model_key、预算和请求类型，不记录业务内容。
5. 预算不足时在网络请求前抛出统一的 `ContextWindowExceededError`，其为 `AppError` 子类。不得将它作为可重试网络故障、空输出或 JSON 格式降级处理。
6. `StructuredOutput` 的 JSON Schema、JSON mode、fallback、回喂和截断扩大输出额度都通过同一个闸门。每次内部请求重新以该次的 messages 与 `max_tokens` 校验；尤其回喂消息和两倍输出预算不得沿用首次结果。
7. **请求预算闸由 LLM 模块内部管理，不新增 `LLMGateway` 调用级标量参数**：装配根 `register_config` 注入窗口/余量，每次真实请求在 `_budget_guarded_call` 内对完整最终 `kwargs` 校验，已覆盖 provider 窗口准入。早期草案曾预留「若调用方需把 `max_context_tokens` 作为硬上限则扩展端口」——因违反「不得为预算闸修改 `LLMGateway` 接口契约」而否决（2026-09-06 修订）。若未来确需把 Application 语义预算作为最终请求硬上限，列为升级路径评估，届时优先复用 Integration 内部机制，不新增端口调用级参数。
8. 取消、总时长和成本继续由策略层的执行护栏管理；`_common.py` 只保留无状态判断和剩余时间计算。实际 `generate_structured` 调用必须获得取消/超时边界，且不得吞 `asyncio.CancelledError`。策略层的绝对截止（`asyncio.timeout` 等）可打断集成层任意 `await`（含限流排队），并走既有 cancel/settle 退款闭环，作为取消的总兜底。（2026-09-08 structured 兑现：`generate_structured` 增加可选 `cancel_event` 与 `deadline`（monotonic 绝对，调用方现算 `start_time + max_execution_time`），`StructuredOutput` 降级链在每条真实子调用前（`_call_generate` 入口，覆盖三级初始/截断扩容/回喂/fallback）检查命中即返回 None——与降级耗尽同出口，reflection/planner 既有 None 降级路径保证终止后无新调用；对注入链内的**内置 `TimeoutError`** 直抛保留执行终止语义，不被 `decide_downstream_error` 误当可恢复网络超时降级再调用（openai `APITimeoutError`/`httpx.TimeoutException` 仍走可恢复路径）。正在进行的单笔 generate（限流排队 / SDK create 中）仍不优雅打断，绝对截止硬取消兜底如本决策所述。）
9. **副模型（fallback 备用链路）按独立 `fallback` 键治理，与主请求共享同一请求生命周期**：fallback 键窗口校验目标（备用）模型 → fallback 独立配额池 `reserve` → `create` → Reservation 写入与主请求同一 `active` 的 `settle`/`cancel`。独立配额池是当前体系默认（`main`/`reasoning`/`fast` 本就按模型键独立桶）；若真实供应商对主/副模型共享配额（如共享模型组限速），应合并记账——待按供应商配额范围核实，作为升级路径。端点复用不变：LLM-012 同 provider 约束仅约束 base_url/密钥复用，不约束窗口与配额（窗口是模型实体属性）。取消竞态：`_budget_guarded_call` 在 `reserve` 返回后、`create` 前复查业务 `cancel_event`，命中则退款且不发起请求；限流排队等待本身不提前打断（绝对截止硬取消兜底，见 Decision 8）。

## Consequences

- ✅ 每次真实 provider 请求均在网络调用前被保护，且各策略共享同一最终口径。
- ✅ 领域层保留 RCA 证据取舍和降级语义；Integration 不擅自删除证据。
- ✅ 结构化重试不会绕过窗口限制。
- ⚠️ 客户端计数不能替代 provider tokenizer；安全余量与按环境可配置的窗口是必要代价。
- ⚠️ 客户端闸仅保证 provider 输入窗口，产品的会话语义预算仍由 `ContextManager` 维护。

## 工业级参照

- [OpenHands LLM adapter](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/llm/llm.py)：Bedrock 的共享窗口和输出压缩由 LLM adapter 按 provider 规则处理。
- [Amazon Bedrock：Claude extended thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html)：超出 `prompt tokens + max_tokens` 的请求被校验拒绝，thinking 与 tool use 进入窗口计算。
- [LangChain Chat Model](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/base.py#L1844-L1919)：模型接口提供 messages/tools 计数，并提示 tools schema 计数的通用限制。
